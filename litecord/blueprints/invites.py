"""

Litecord
Copyright (C) 2018-2019  Luna Mendes

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, version 3 of the License.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""

import re
import secrets
import datetime

from quart import Blueprint, request, current_app as app, jsonify
from logbook import Logger

from ..auth import token_check
from ..schemas import validate, INVITE
from ..enums import ChannelType
from ..errors import BadRequest, Forbidden
from ..utils import async_map

from litecord.blueprints.checks import (
    channel_check,
    channel_perm_check,
    guild_check,
    guild_perm_check,
)

from litecord.blueprints.dm_channels import gdm_is_member, gdm_add_recipient
from litecord.common.guilds import create_guild_settings

log = Logger(__name__)
bp = Blueprint("invites", __name__)


class UnknownInvite(BadRequest):
    error_code = 10006


class InvalidInvite(Forbidden):
    error_code = 50020


class AlreadyInvited(BaseException):
    pass


def gen_inv_code() -> str:
    """Generate an invite code.

    This is a primitive and does not guarantee uniqueness.
    """
    raw = secrets.token_urlsafe(10)
    raw = re.sub(r"\/|\+|\-|\_", "", raw)

    return raw[:7]


async def invite_precheck(user_id: int, guild_id: int):
    """pre-check invite use in the context of a guild."""

    joined = await app.db.fetchval(
        """
    SELECT joined_at
    FROM members
    WHERE user_id = $1 AND guild_id = $2
    """,
        user_id,
        guild_id,
    )

    if joined is not None:
        raise AlreadyInvited("You are already in the guild")

    banned = await app.db.fetchval(
        """
    SELECT reason
    FROM bans
    WHERE user_id = $1 AND guild_id = $2
    """,
        user_id,
        guild_id,
    )

    if banned is not None:
        raise InvalidInvite("You are banned.")


async def invite_precheck_gdm(user_id: int, channel_id: int):
    """pre-checks in a group dm."""
    is_member = await gdm_is_member(channel_id, user_id)

    if is_member:
        raise AlreadyInvited("You are already in the Group DM")


async def _inv_check_age(inv: dict):
    if inv["max_age"] == 0:
        return

    now = datetime.datetime.utcnow()
    delta_sec = (now - inv["created_at"]).total_seconds()

    if delta_sec > inv["max_age"]:
        await delete_invite(inv["code"])
        raise InvalidInvite("Invite is expired")

    if inv["max_uses"] is not -1 and inv["uses"] > inv["max_uses"]:
        await delete_invite(inv["code"])
        raise InvalidInvite("Too many uses")


async def _guild_add_member(guild_id: int, user_id: int):
    """Add a user to a guild.

    Dispatches:
     - GUILD_MEMBER_ADD to all members.
     - lazy guild events for member add.
     - subscribes the peer to the guild.
     - dispatches a GUILD_CREATE to the peer.
    """

    # TODO: system message for member join
    await app.db.execute(
        """
    INSERT INTO members (user_id, guild_id)
    VALUES ($1, $2)
    """,
        user_id,
        guild_id,
    )

    await create_guild_settings(guild_id, user_id)

    # add the @everyone role to the invited member
    await app.db.execute(
        """
    INSERT INTO member_roles (user_id, guild_id, role_id)
    VALUES ($1, $2, $3)
    """,
        user_id,
        guild_id,
        guild_id,
    )

    # tell current members a new member came up
    member = await app.storage.get_member_data_one(guild_id, user_id)
    await app.dispatcher.dispatch_guild(
        guild_id, "GUILD_MEMBER_ADD", {**member, **{"guild_id": str(guild_id)}}
    )

    # update member lists for the new member
    await app.dispatcher.dispatch("lazy_guild", guild_id, "new_member", user_id)

    # subscribe new member to guild, so they get events n stuff
    await app.dispatcher.sub("guild", guild_id, user_id)

    # tell the new member that theres the guild it just joined.
    # we use dispatch_user_guild so that we send the GUILD_CREATE
    # just to the shards that are actually tied to it.
    guild = await app.storage.get_guild_full(guild_id, user_id, 250)
    await app.dispatcher.dispatch_user_guild(user_id, guild_id, "GUILD_CREATE", guild)


async def use_invite(user_id, invite_code):
    """Try using an invite"""
    inv = await app.db.fetchrow(
        """
    SELECT code, channel_id, guild_id, created_at,
           max_age, uses, max_uses
    FROM invites
    WHERE code = $1
    """,
        invite_code,
    )

    if inv is None:
        raise UnknownInvite("Unknown invite")

    await _inv_check_age(inv)

    # NOTE: if group dm invite, guild_id is null.
    guild_id = inv["guild_id"]

    try:
        if guild_id is None:
            channel_id = inv["channel_id"]
            await invite_precheck_gdm(user_id, inv["channel_id"])
            await gdm_add_recipient(channel_id, user_id)
        else:
            await invite_precheck(user_id, guild_id)
            await _guild_add_member(guild_id, user_id)

        await app.db.execute(
            """
        UPDATE invites
        SET uses = uses + 1
        WHERE code = $1
        """,
            invite_code,
        )
    except AlreadyInvited:
        pass


@bp.route("/channels/<int:channel_id>/invites", methods=["POST"])
async def create_invite(channel_id):
    """Create an invite to a channel."""
    user_id = await token_check()
    j = validate(await request.get_json(), INVITE)

    chantype, maybe_guild_id = await channel_check(user_id, channel_id)
    chantype = ChannelType(chantype)

    # NOTE: this works on group dms, since it returns ALL_PERMISSIONS on
    # non-guild channels.
    await channel_perm_check(user_id, channel_id, "create_invites")

    if chantype not in (
        ChannelType.GUILD_TEXT,
        ChannelType.GUILD_VOICE,
        ChannelType.GROUP_DM,
    ):
        raise BadRequest("Invalid channel type")

    invite_code = gen_inv_code()

    if chantype in (ChannelType.GUILD_TEXT, ChannelType.GUILD_VOICE):
        guild_id = maybe_guild_id
    else:
        guild_id = None

    await app.db.execute(
        """
        INSERT INTO invites
            (code, guild_id, channel_id, inviter, max_uses,
            max_age, temporary)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        invite_code,
        guild_id,
        channel_id,
        user_id,
        j["max_uses"],
        j["max_age"],
        j["temporary"],
    )

    invite = await app.storage.get_invite(invite_code)
    return jsonify(invite)


@bp.route("/invite/<invite_code>", methods=["GET"])
@bp.route("/invites/<invite_code>", methods=["GET"])
async def get_invite(invite_code: str):
    inv = await app.storage.get_invite(invite_code)

    if not inv:
        return "", 404

    if request.args.get("with_counts"):
        extra = await app.storage.get_invite_extra(invite_code)
        inv.update(extra)

    return jsonify(inv)


async def delete_invite(invite_code: str):
    """Delete an invite."""
    await app.db.fetchval(
        """
    DELETE FROM invites
    WHERE code = $1
    """,
        invite_code,
    )


@bp.route("/invite/<invite_code>", methods=["DELETE"])
@bp.route("/invites/<invite_code>", methods=["DELETE"])
async def _delete_invite(invite_code: str):
    user_id = await token_check()

    guild_id = await app.db.fetchval(
        """
    SELECT guild_id
    FROM invites
    WHERE code = $1
    """,
        invite_code,
    )

    if guild_id is None:
        raise BadRequest("Unknown invite")

    await guild_perm_check(user_id, guild_id, "manage_channels")

    inv = await app.storage.get_invite(invite_code)
    await delete_invite(invite_code)
    return jsonify(inv)


async def _get_inv(code):
    inv = await app.storage.get_invite(code)
    meta = await app.storage.get_invite_metadata(code)
    return {**inv, **meta}


@bp.route("/guilds/<int:guild_id>/invites", methods=["GET"])
async def get_guild_invites(guild_id: int):
    """Get all invites for a guild."""
    user_id = await token_check()

    await guild_check(user_id, guild_id)
    await guild_perm_check(user_id, guild_id, "manage_guild")

    inv_codes = await app.db.fetch(
        """
    SELECT code
    FROM invites
    WHERE guild_id = $1
    """,
        guild_id,
    )

    inv_codes = [r["code"] for r in inv_codes]
    invs = await async_map(_get_inv, inv_codes)
    return jsonify(invs)


@bp.route("/channels/<int:channel_id>/invites", methods=["GET"])
async def get_channel_invites(channel_id: int):
    """Get all invites for a channel."""
    user_id = await token_check()

    _ctype, guild_id = await channel_check(user_id, channel_id)
    await guild_perm_check(user_id, guild_id, "manage_channels")

    inv_codes = await app.db.fetch(
        """
    SELECT code
    FROM invites
    WHERE guild_id = $1 AND channel_id = $2
    """,
        guild_id,
        channel_id,
    )

    inv_codes = [r["code"] for r in inv_codes]
    invs = await async_map(_get_inv, inv_codes)
    return jsonify(invs)


@bp.route("/invite/<invite_code>", methods=["POST"])
@bp.route("/invites/<invite_code>", methods=["POST"])
async def _use_invite(invite_code):
    """Use an invite."""
    user_id = await token_check()

    await use_invite(user_id, invite_code)

    # the reply is an invite object for some reason.
    inv = await app.storage.get_invite(invite_code)
    inv_meta = await app.storage.get_invite_metadata(invite_code)

    return jsonify({**inv, **{"inviter": inv_meta["inviter"]}})
