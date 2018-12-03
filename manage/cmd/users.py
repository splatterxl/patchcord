import base64
import itsdangerous
import bcrypt
from litecord.blueprints.auth import create_user, make_token
from litecord.enums import UserFlags


async def find_user(username, discrim, ctx):
    return await ctx.db.fetchval("""
    SELECT id
    FROM users
    WHERE username = $1 AND discriminator = $2
    """, username, discrim)

async def get_password_hash(id, ctx):
    return await ctx.db.fetchval("""
    SELECT password_hash
    FROM users
    WHERE id = $1
    """, id)


async def set_user_staff(user_id, ctx):
    """Give a single user staff status."""
    old_flags = await ctx.db.fetchval("""
    SELECT flags
    FROM users
    WHERE id = $1
    """, user_id)

    new_flags = old_flags | UserFlags.staff

    await ctx.db.execute("""
    UPDATE users
    SET flags=$1
    WHERE id = $2
    """, new_flags, user_id)


async def adduser(ctx, args):
    """Create a single user."""
    uid, _ = await create_user(args.username, args.email,
                               args.password, ctx.db, ctx.loop)

    print('created!')
    print(f'\tuid: {uid}')


async def make_staff(ctx, args):
    """Give a single user the staff flag.

    This will grant them access to the Admin API.

    The flag changes will only apply after a
    server restart.
    """
    uid = await find_user(args.username, args.discrim, ctx)

    if not uid:
        return print('user not found')

    await set_user_staff(uid, ctx)
    print('OK: set staff')

async def generate_token(ctx, args):
    """Generate a token for specified user."""
    uid = await find_user(args.username, args.discrim, ctx)

    if not uid:
        return print('user not found')
    
    password_hash = await get_password_hash(uid, ctx)
    print(make_token(uid, password_hash))


def setup(subparser):
    setup_test_parser = subparser.add_parser(
        'adduser',
        help='create a user',
    )

    setup_test_parser.add_argument(
        'username', help='username of the user')
    setup_test_parser.add_argument(
        'email', help='email of the user')
    setup_test_parser.add_argument(
        'password', help='password of the user')

    setup_test_parser.set_defaults(func=adduser)

    staff_parser = subparser.add_parser(
        'make_staff',
        help='make a user staff',
        description=make_staff.__doc__
    )

    staff_parser.add_argument(
        'username'
    )
    staff_parser.add_argument(
        'discrim', help='the discriminator of the user'
    )

    staff_parser.set_defaults(func=make_staff)

    token_parser = subparser.add_parser(
        'generate_token',
        help='generate a token for specified user',
        description=generate_token.__doc__
    )

    token_parser.add_argument(
        'username'
    )
    token_parser.add_argument(
        'discrim', help='the discriminator of the user'
    )

    token_parser.set_defaults(func=generate_token)
