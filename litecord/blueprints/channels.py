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

import time
from typing import List, Optional

from quart import Blueprint, request, current_app as app, jsonify
from logbook import Logger

from litecord.auth import token_check
from litecord.enums import ChannelType, GUILD_CHANS, MessageType
from litecord.errors import ChannelNotFound, Forbidden
from litecord.schemas import (
    validate, CHAN_UPDATE, CHAN_OVERWRITE, SEARCH_CHANNEL, GROUP_DM_UPDATE
)

from litecord.blueprints.checks import channel_check, channel_perm_check
from litecord.system_messages import send_sys_message
from litecord.blueprints.dm_channels import (
    gdm_remove_recipient, gdm_destroy
)
from litecord.utils import search_result_from_list
from litecord.embed.messages import process_url_embed, msg_update_embeds

log = Logger(__name__)
bp = Blueprint('channels', __name__)


@bp.route('/<int:channel_id>', methods=['GET'])
async def get_channel(channel_id):
    """Get a single channel's information"""
    user_id = await token_check()

    # channel_check takes care of checking
    # DMs and group DMs
    await channel_check(user_id, channel_id)
    chan = await app.storage.get_channel(channel_id)

    if not chan:
        raise ChannelNotFound('single channel not found')

    return jsonify(chan)


async def __guild_chan_sql(guild_id, channel_id, field: str) -> str:
    """Update a guild's channel id field to NULL,
    if it was set to the given channel id before."""
    return await app.db.execute(f"""
    UPDATE guilds
    SET {field} = NULL
    WHERE guilds.id = $1 AND {field} = $2
    """, guild_id, channel_id)


async def _update_guild_chan_text(guild_id: int, channel_id: int):
    res_embed = await __guild_chan_sql(
        guild_id, channel_id, 'embed_channel_id')

    res_widget = await __guild_chan_sql(
        guild_id, channel_id, 'widget_channel_id')

    res_system = await __guild_chan_sql(
        guild_id, channel_id, 'system_channel_id')

    # if none of them were actually updated,
    # ignore and dont dispatch anything
    if 'UPDATE 1' not in (res_embed, res_widget, res_system):
        return

    # at least one of the fields were updated,
    # dispatch GUILD_UPDATE
    guild = await app.storage.get_guild(guild_id)
    await app.dispatcher.dispatch_guild(
        guild_id, 'GUILD_UPDATE', guild)


async def _update_guild_chan_voice(guild_id: int, channel_id: int):
    res = await __guild_chan_sql(guild_id, channel_id, 'afk_channel_id')

    # guild didnt update
    if res == 'UPDATE 0':
        return

    guild = await app.storage.get_guild(guild_id)
    await app.dispatcher.dispatch_guild(
        guild_id, 'GUILD_UPDATE', guild)


async def _update_guild_chan_cat(guild_id: int, channel_id: int):
    # get all channels that were childs of the category
    childs = await app.db.fetch("""
    SELECT id
    FROM guild_channels
    WHERE guild_id = $1 AND parent_id = $2
    """, guild_id, channel_id)
    childs = [c['id'] for c in childs]

    # update every child channel to parent_id = NULL
    await app.db.execute("""
    UPDATE guild_channels
    SET parent_id = NULL
    WHERE guild_id = $1 AND parent_id = $2
    """, guild_id, channel_id)

    # tell all people in the guild of the category removal
    for child_id in childs:
        child = await app.storage.get_channel(child_id)
        await app.dispatcher.dispatch_guild(
            guild_id, 'CHANNEL_UPDATE', child
        )


async def delete_messages(channel_id):
    await app.db.execute("""
    DELETE FROM channel_pins
    WHERE channel_id = $1
    """, channel_id)

    await app.db.execute("""
    DELETE FROM user_read_state
    WHERE channel_id = $1
    """, channel_id)

    await app.db.execute("""
    DELETE FROM messages
    WHERE channel_id = $1
    """, channel_id)


async def guild_cleanup(channel_id):
    await app.db.execute("""
    DELETE FROM channel_overwrites
    WHERE channel_id = $1
    """, channel_id)

    await app.db.execute("""
    DELETE FROM invites
    WHERE channel_id = $1
    """, channel_id)

    await app.db.execute("""
    DELETE FROM webhooks
    WHERE channel_id = $1
    """, channel_id)


@bp.route('/<int:channel_id>', methods=['DELETE'])
async def close_channel(channel_id):
    """Close or delete a channel."""
    user_id = await token_check()

    chan_type = await app.storage.get_chan_type(channel_id)
    ctype = ChannelType(chan_type)

    if ctype in GUILD_CHANS:
        _, guild_id = await channel_check(user_id, channel_id)
        chan = await app.storage.get_channel(channel_id)

        # the selected function will take care of checking
        # the sanity of tables once the channel becomes deleted.
        _update_func = {
            ChannelType.GUILD_TEXT: _update_guild_chan_text,
            ChannelType.GUILD_VOICE: _update_guild_chan_voice,
            ChannelType.GUILD_CATEGORY: _update_guild_chan_cat,
        }[ctype]

        main_tbl = {
            ChannelType.GUILD_TEXT: 'guild_text_channels',
            ChannelType.GUILD_VOICE: 'guild_voice_channels',

            # TODO: categories?
        }[ctype]

        await _update_func(guild_id, channel_id)

        # for some reason ON DELETE CASCADE
        # didn't work on my setup, so I delete
        # everything before moving to the main
        # channel table deletes
        await delete_messages(channel_id)
        await guild_cleanup(channel_id)

        await app.db.execute(f"""
        DELETE FROM {main_tbl}
        WHERE id = $1
        """, channel_id)

        await app.db.execute("""
        DELETE FROM guild_channels
        WHERE id = $1
        """, channel_id)

        await app.db.execute("""
        DELETE FROM channels
        WHERE id = $1
        """, channel_id)

        # clean its member list representation
        lazy_guilds = app.dispatcher.backends['lazy_guild']
        lazy_guilds.remove_channel(channel_id)

        await app.dispatcher.dispatch_guild(
            guild_id, 'CHANNEL_DELETE', chan)

        await app.dispatcher.remove('channel', channel_id)
        return jsonify(chan)

    if ctype == ChannelType.DM:
        chan = await app.storage.get_channel(channel_id)

        # we don't ever actually delete DM channels off the database.
        # instead, we close the channel for the user that is making
        # the request via removing the link between them and
        # the channel on dm_channel_state
        await app.db.execute("""
        DELETE FROM dm_channel_state
        WHERE user_id = $1 AND dm_id = $2
        """, user_id, channel_id)

        # unsubscribe
        await app.dispatcher.unsub('channel', channel_id, user_id)

        # nothing happens to the other party of the dm channel
        await app.dispatcher.dispatch_user(user_id, 'CHANNEL_DELETE', chan)

        return jsonify(chan)

    if ctype == ChannelType.GROUP_DM:
        await gdm_remove_recipient(channel_id, user_id)

        gdm_count = await app.db.fetchval("""
        SELECT COUNT(*)
        FROM group_dm_members
        WHERE id = $1
        """, channel_id)

        if gdm_count == 0:
            # destroy dm
            await gdm_destroy(channel_id)

    raise ChannelNotFound()


async def _update_pos(channel_id, pos: int):
    await app.db.execute("""
    UPDATE guild_channels
    SET position = $1
    WHERE id = $2
    """, pos, channel_id)


async def _mass_chan_update(guild_id, channel_ids: List[Optional[int]]):
    for channel_id in channel_ids:
        if channel_id is None:
            continue

        chan = await app.storage.get_channel(channel_id)
        await app.dispatcher.dispatch(
            'guild', guild_id, 'CHANNEL_UPDATE', chan)


async def _process_overwrites(channel_id: int, overwrites: list):
    for overwrite in overwrites:

        # 0 for member overwrite, 1 for role overwrite
        target_type = 0 if overwrite['type'] == 'member' else 1
        target_role = None if target_type == 0 else overwrite['id']
        target_user = overwrite['id'] if target_type == 0 else None

        col_name = 'target_user' if target_type == 0 else 'target_role'
        constraint_name = f'channel_overwrites_target_{col_name}'

        await app.db.execute(
            f"""
            INSERT INTO channel_overwrites
                (channel_id, target_type, target_role,
                target_user, allow, deny)
            VALUES
                ($1, $2, $3, $4, $5, $6)
            ON CONFLICT ON CONSTRAINT {constraint_name}
            DO
            UPDATE
                SET allow = $5, deny = $6
                WHERE channel_overwrites.channel_id = $1
                  AND channel_overwrites.target_type = $2
                  AND channel_overwrites.target_role = $3
                  AND channel_overwrites.target_user = $4
            """,
            channel_id, target_type,
            target_role, target_user,
            overwrite['allow'], overwrite['deny'])


@bp.route('/<int:channel_id>/permissions/<int:overwrite_id>', methods=['PUT'])
async def put_channel_overwrite(channel_id: int, overwrite_id: int):
    """Insert or modify a channel overwrite."""
    user_id = await token_check()
    ctype, guild_id = await channel_check(user_id, channel_id)

    if ctype not in GUILD_CHANS:
        raise ChannelNotFound('Only usable for guild channels.')

    await channel_perm_check(user_id, guild_id, 'manage_roles')

    j = validate(
        # inserting a fake id on the payload so validation passes through
        {**await request.get_json(), **{'id': -1}},
        CHAN_OVERWRITE
    )

    await _process_overwrites(channel_id, [{
        'allow': j['allow'],
        'deny': j['deny'],
        'type': j['type'],
        'id': overwrite_id
    }])

    await _mass_chan_update(guild_id, [channel_id])
    return '', 204


async def _update_channel_common(channel_id, guild_id: int, j: dict):
    if 'name' in j:
        await app.db.execute("""
        UPDATE guild_channels
        SET name = $1
        WHERE id = $2
        """, j['name'], channel_id)

    if 'position' in j:
        channel_data = await app.storage.get_channel_data(guild_id)

        chans = [None] * len(channel_data)
        for chandata in channel_data:
            chans.insert(chandata['position'], int(chandata['id']))

        # are we changing to the left or to the right?

        # left: [channel1, channel2, ..., channelN-1, channelN]
        #       becomes
        #       [channel1, channelN-1, channel2, ..., channelN]
        #       so we can say that the "main change" is
        #       channelN-1 going to the position channel2
        #       was occupying.
        current_pos = chans.index(channel_id)
        new_pos = j['position']

        # if the new position is bigger than the current one,
        # we're making a left shift of all the channels that are
        # beyond the current one, to make space
        left_shift = new_pos > current_pos

        # find all channels that we'll have to shift
        shift_block = (chans[current_pos:new_pos]
                       if left_shift else
                       chans[new_pos:current_pos]
                       )

        shift = -1 if left_shift else 1

        # do the shift (to the left or to the right)
        await app.db.executemany("""
        UPDATE guild_channels
        SET position = position + $1
        WHERE id = $2
        """, [(shift, chan_id) for chan_id in shift_block])

        await _mass_chan_update(guild_id, shift_block)

        # since theres now an empty slot, move current channel to it
        await _update_pos(channel_id, new_pos)

    if 'channel_overwrites' in j:
        overwrites = j['channel_overwrites']
        await _process_overwrites(channel_id, overwrites)


async def _common_guild_chan(channel_id, j: dict):
    # common updates to the guild_channels table
    for field in [field for field in j.keys()
                  if field in ('nsfw', 'parent_id')]:
        await app.db.execute(f"""
        UPDATE guild_channels
        SET {field} = $1
        WHERE id = $2
        """, j[field], channel_id)


async def _update_text_channel(channel_id: int, j: dict, _user_id: int):
    # first do the specific ones related to guild_text_channels
    for field in [field for field in j.keys()
                  if field in ('topic', 'rate_limit_per_user')]:
        await app.db.execute(f"""
        UPDATE guild_text_channels
        SET {field} = $1
        WHERE id = $2
        """, j[field], channel_id)

    await _common_guild_chan(channel_id, j)


async def _update_voice_channel(channel_id: int, j: dict, _user_id: int):
    # first do the specific ones in guild_voice_channels
    for field in [field for field in j.keys()
                  if field in ('bitrate', 'user_limit')]:
        await app.db.execute(f"""
        UPDATE guild_voice_channels
        SET {field} = $1
        WHERE id = $2
        """, j[field], channel_id)

    # yes, i'm letting voice channels have nsfw, you cant stop me
    await _common_guild_chan(channel_id, j)


async def _update_group_dm(channel_id: int, j: dict, author_id: int):
    if 'name' in j:
        await app.db.execute("""
        UPDATE group_dm_channels
        SET name = $1
        WHERE id = $2
        """, j['name'], channel_id)

        await send_sys_message(
            app, channel_id, MessageType.CHANNEL_NAME_CHANGE, author_id
        )

    if 'icon' in j:
        new_icon = await app.icons.update(
            'channel-icons', channel_id, j['icon'], always_icon=True
        )

        await app.db.execute("""
        UPDATE group_dm_channels
        SET icon = $1
        WHERE id = $2
        """, new_icon.icon_hash, channel_id)

        await send_sys_message(
            app, channel_id, MessageType.CHANNEL_ICON_CHANGE, author_id
        )


@bp.route('/<int:channel_id>', methods=['PUT', 'PATCH'])
async def update_channel(channel_id):
    """Update a channel's information"""
    user_id = await token_check()
    ctype, guild_id = await channel_check(user_id, channel_id)

    if ctype not in (ChannelType.GUILD_TEXT, ChannelType.GUILD_VOICE,
                     ChannelType.GROUP_DM):
        raise ChannelNotFound('unable to edit unsupported chan type')

    is_guild = ctype in GUILD_CHANS

    if is_guild:
        await channel_perm_check(user_id, channel_id, 'manage_channels')

    j = validate(await request.get_json(),
                 CHAN_UPDATE if is_guild else GROUP_DM_UPDATE)

    # TODO: categories
    update_handler = {
        ChannelType.GUILD_TEXT: _update_text_channel,
        ChannelType.GUILD_VOICE: _update_voice_channel,
        ChannelType.GROUP_DM: _update_group_dm,
    }[ctype]

    if is_guild:
        await _update_channel_common(channel_id, guild_id, j)

    await update_handler(channel_id, j, user_id)

    chan = await app.storage.get_channel(channel_id)

    if is_guild:
        await app.dispatcher.dispatch(
            'guild', guild_id, 'CHANNEL_UPDATE', chan)
    else:
        await app.dispatcher.dispatch(
            'channel', channel_id, 'CHANNEL_UPDATE', chan)

    return jsonify(chan)


@bp.route('/<int:channel_id>/typing', methods=['POST'])
async def trigger_typing(channel_id):
    user_id = await token_check()
    ctype, guild_id = await channel_check(user_id, channel_id)

    await app.dispatcher.dispatch('channel', channel_id, 'TYPING_START', {
        'channel_id': str(channel_id),
        'user_id': str(user_id),
        'timestamp': int(time.time()),

        # guild_id for lazy guilds
        'guild_id': str(guild_id) if ctype == ChannelType.GUILD_TEXT else None,
    })

    return '', 204


async def channel_ack(user_id, guild_id, channel_id, message_id: int = None):
    """ACK a channel."""

    if not message_id:
        message_id = await app.storage.chan_last_message(channel_id)

    await app.db.execute("""
    INSERT INTO user_read_state
        (user_id, channel_id, last_message_id, mention_count)
    VALUES
        ($1, $2, $3, 0)
    ON CONFLICT ON CONSTRAINT user_read_state_pkey
    DO
      UPDATE
        SET last_message_id = $3, mention_count = 0
        WHERE user_read_state.user_id = $1
          AND user_read_state.channel_id = $2
    """, user_id, channel_id, message_id)

    if guild_id:
        await app.dispatcher.dispatch_user_guild(
            user_id, guild_id, 'MESSAGE_ACK', {
                'message_id': str(message_id),
                'channel_id': str(channel_id)
            })
    else:
        # we don't use ChannelDispatcher here because since
        # guild_id is None, all user devices are already subscribed
        # to the given channel (a dm or a group dm)
        await app.dispatcher.dispatch_user(
            user_id, 'MESSAGE_ACK', {
                'message_id': str(message_id),
                'channel_id': str(channel_id)
            })


@bp.route('/<int:channel_id>/messages/<int:message_id>/ack', methods=['POST'])
async def ack_channel(channel_id, message_id):
    """Acknowledge a channel."""
    user_id = await token_check()
    ctype, guild_id = await channel_check(user_id, channel_id)

    if ctype == ChannelType.DM:
        guild_id = None

    await channel_ack(user_id, guild_id, channel_id, message_id)

    return jsonify({
        # token seems to be used for
        # data collection activities,
        # so we never use it.
        'token': None
    })


@bp.route('/<int:channel_id>/messages/ack', methods=['DELETE'])
async def delete_read_state(channel_id):
    """Delete the read state of a channel."""
    user_id = await token_check()
    await channel_check(user_id, channel_id)

    await app.db.execute("""
    DELETE FROM user_read_state
    WHERE user_id = $1 AND channel_id = $2
    """, user_id, channel_id)

    return '', 204


@bp.route('/<int:channel_id>/messages/search', methods=['GET'])
async def _search_channel(channel_id):
    """Search in DMs or group DMs"""
    user_id = await token_check()
    await channel_check(user_id, channel_id)
    await channel_perm_check(user_id, channel_id, 'read_messages')

    j = validate(dict(request.args), SEARCH_CHANNEL)

    # main search query
    # the context (before/after) columns are copied from the guilds blueprint.
    rows = await app.db.fetch(f"""
    SELECT orig.id AS current_id,
        COUNT(*) OVER() AS total_results,
        array((SELECT messages.id AS before_id
         FROM messages WHERE messages.id < orig.id
         ORDER BY messages.id DESC LIMIT 2)) AS before,
        array((SELECT messages.id AS after_id
         FROM messages WHERE messages.id > orig.id
         ORDER BY messages.id ASC LIMIT 2)) AS after

    FROM messages AS orig
    WHERE channel_id = $1
      AND content LIKE '%'||$3||'%'
    ORDER BY orig.id DESC
    LIMIT 50
    OFFSET $2
    """, channel_id, j['offset'], j['content'])

    return jsonify(await search_result_from_list(rows))


@bp.route('/<int:channel_id>/messages/<int:message_id>/suppress-embeds',
          methods=['POST'])
async def suppress_embeds(channel_id: int, message_id: int):
    """Toggle the embeds in a message.
    
    Either the author of the message or a channel member with the
    Manage Messages permission can run this route.
    """
    user_id = await token_check()
    await channel_check(user_id, channel_id)

    # the checks here have been copied from the delete_message()
    # handler on blueprints.channel.messages. maybe we can combine
    # them someday?
    author_id = await app.db.fetchval("""
    SELECT author_id FROM messages
    WHERE messages.id = $1
    """, message_id)

    by_perms = await channel_perm_check(
        user_id, channel_id, 'manage_messages', False)

    by_author = author_id == user_id

    can_suppress = by_perms or by_author
    if not can_suppress:
        raise Forbidden('Not enough permissions.')

    j = validate(
        await request.get_json(),
        {'suppress': {'type': 'boolean'}},
    )

    suppress = j['suppress']
    message = await app.storage.get_message(message_id)
    url_embeds = sum(
        1 for embed in message['embeds'] if embed['type'] == 'url')

    if suppress and url_embeds:
        # delete all embeds then dispatch an update
        await msg_update_embeds(message, [], app.storage, app.dispatcher)
    elif not suppress and not url_embeds:
        # spawn process_url_embed to restore the embeds, if any
        app.sched.spawn(
            process_url_embed(
                app.config, app.storage, app.dispatcher, app.session,
                message
            )
        )

    return '', 204
