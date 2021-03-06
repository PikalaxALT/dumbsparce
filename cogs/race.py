import discord
from discord.ext import commands, tasks
import asyncpg
import typing
import time
import base64
import asyncio
import datetime


class NotEnoughRacers:
    pass


class NotReady:
    pass


class NoGuildConfig:
    pass


class GuildConfigExists:
    pass


class RaceDoesNotExist:
    pass


class NotHost:
    pass


class RaceNotStarted:
    pass


class RaceAlreadyStarted:
    pass


class NotRacing:
    pass


class Race(commands.Cog):
    @staticmethod
    def gen_hash(timestamp):
        return base64.b32encode(hash(timestamp).to_bytes(8, 'little')).decode().rstrip('=')

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.db: typing.Optional[asyncpg.Connection] = None
        asyncio.create_task(self.init_db())

    async def init_db(self):
        self.db = await asyncpg.connect(user=self.bot.postgre_user, password=self.bot.postgre_pass)
        async with self.db.transaction():
            await self.db.execute("""
            CREATE TABLE IF NOT EXISTS config(
                guild BIGINT PRIMARY KEY,
                category BIGINT NOT NULL,
                archive BIGINT NOT NULL
            )
            """)
            await self.db.execute("""
            CREATE TABLE IF NOT EXISTS races(
                hash TEXT PRIMARY KEY NOT NULL,
                host BIGINT NOT NULL,
                started TIMESTAMP,
                channel BIGINT NOT NULL,
                role BIGINT NOT NULL,
                voicechan BIGINT
            )
            """)
            await self.db.execute("""
            CREATE TABLE IF NOT EXISTS racers(
                hash text PRIMARY KEY NOT NULL,
                id BIGINT NOT NULL,
                ishost BOOLEAN DEFAULT FALSE,
                finished TIMESTAMP
            )
            """)

    async def _get_guild_config(self, ctx: commands.Context):
        category = await self.db.fetchrow("""
            SELECT category FROM config WHERE (guild = $1)
        """, ctx.guild.id)
        if category is None:
            raise NoGuildConfig
        return category

    async def _get_race_settings(self, ctx: commands.Context, code=None):
        if code is not None:
            record = await self.db.fetchrow("""
                SELECT (*) from races where hash = $1
            """, code)
        else:
            record = await self.db.fetchrow("""
                SELECT (*) from races where channel = $1
            """, ctx.channel.id)
        if record is None:
            raise RaceDoesNotExist
        return record

    async def _get_racers(self, ctx: commands.Context, code=None):
        if code is None:
            code = (await self._get_race_settings(ctx))['hash']
        records = await self.db.fetch("""
            SELECT (id, ishost, finished) FROM racers WHERE hash = $1
        """, code)
        if records is None:
            raise RaceDoesNotExist
        return records

    async def _get_racer(self, ctx: commands.Context, code=None):
        if code is None:
            code = (await self._get_race_settings(ctx))['hash']
        record = await self.db.fetchrow("""
            SELECT (id, ishost, finished) FROM racers WHERE hash = $1 AND id = $2
        """, code, ctx.author.id)
        if record is None:
            raise NotRacing
        return record

    async def guild_has_category(self, ctx):
        await self._get_guild_config(ctx)
        return True

    async def guild_has_no_category(self, ctx):
        try:
            await self._get_guild_config(ctx)
        except NoGuildConfig:
            return True
        raise GuildConfigExists

    async def is_host(self, ctx):
        record = await self._get_race_settings(ctx)
        flag = await self.db.fetchval("""
            SELECT ishost FROM $1 where id = $2
        """, record['hash'], ctx.author.id)
        if not flag:
            raise NotHost
        return True

    async def is_started(self, ctx):
        record = await self._get_race_settings(ctx)
        if record['started'] is None:
            raise RaceNotStarted
        return True

    async def is_not_started(self, ctx):
        record = await self._get_race_settings(ctx)
        if record['started'] is None:
            return True
        return RaceAlreadyStarted

    async def is_racing(self, ctx):
        record = await self._get_racer(ctx)
        if record['finished'] is not None:
            raise NotRacing
        return True

    @commands.command()
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True, manage_members=True)
    @commands.has_permissions(administrator=True)
    @commands.check(guild_has_no_category)
    async def config(self, ctx):
        overwrites = discord.PermissionOverwrite(send_messages=False, read_messages=False, connect=False, speak=False)
        category = await ctx.guild.create_category_channel('Races', overwrites=overwrites)
        category2 = await ctx.guild.create_category_channel('Race Archive')
        await self.db.execute("""
            INSERT INTO config(guild, category, archive) VALUES ($1, $2, $3)
        """, ctx.guild.id, category.id, category2.id)

    @commands.command(name='race')
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True, manage_members=True)
    @commands.check(guild_has_category)
    async def new_race(self, ctx: commands.Context, tourney=False):
        category = ctx.guild.get_channel((await self._get_guild_config(ctx))['category'])
        now = time.time()
        code = Race.gen_hash(now)
        role = await ctx.guild.create_role(name=f'Racer {code}')
        overwrites = discord.PermissionOverwrite(send_messages=True, read_messages=True, connect=True, speak=True)
        channel = await category.create_text_channel(f'race-{code.lower()}', overwrites={role: overwrites})
        if not tourney:
            vc = await category.create_voice_channel(f'Race Comms {code}', overwrites={role: overwrites})
        else:
            vc = None
        async with self.db.transaction():
            await self.db.execute("""
                INSERT INTO races(code, host, channel, role, voicechan) VALUES ($1, $2, $3, $4, $5)
            """, code, ctx.author.id, channel.id, role.id, vc.id)
            await self.db.execute("""
                INSERT INTO racers(hash, id, ishost) VALUES ($1, $2, TRUE)
            """, code, ctx.author.id)
        await ctx.author.add_roles(role)
        await ctx.send(f'New race channel {channel.mention} created. To join, type `{ctx.prefix}{self.join} {code}`')
        await channel.send(f'{ctx.author.mention}: You are the host of this race. When everyone has joined in, '
                           f'type `{ctx.prefix}{self.start}` to start the race.')
        await channel.send(f'To toggle your ready state, type `{ctx.prefix}{self.ready}.')

    @commands.command()
    @commands.bot_has_permissions(manage_members=True)
    @commands.check(guild_has_category)
    async def join(self, ctx: commands.Context, code):
        category = await self._get_guild_config(ctx)
        record = await self._get_race_settings(ctx, code)
        if record['started'] is not None:
            raise RaceAlreadyStarted
        await self.db.execute("""
            INSERT INTO racers(hash, id) VALUES ($1, $2)
        """, code, ctx.author.id)
        channel = category.get_channel(record['channel'])
        role = ctx.guild.get_role(record['role'])
        await ctx.author.add_roles(role)
        await channel.send(f'Player {ctx.author.mention} has joined the race!')

    @commands.command()
    @commands.check(is_not_started)
    async def ready(self, ctx: commands.Context):
        record = await self._get_race_settings(ctx)
        racer = await self._get_racer(ctx, record['hash'])
        await self.db.execute("""
            UPDATE $1 SET ready=NOT ready where id=$2
        """, record['hash'], ctx.author.id)
        if racer['ready']:
            await ctx.send(f'{ctx.author.display_name} is no longer ready to start.')
        else:
            await ctx.send(f'{ctx.author.display_name} is ready to start!')

    @commands.command()
    @commands.check(guild_has_category)
    @commands.check(is_host)
    @commands.check(is_not_started)
    async def start(self, ctx: commands.Context):
        record = await self._get_race_settings(ctx)
        racers = await self._get_racers(ctx, record['hash'])
        if len(racers) < 2:
            raise NotEnoughRacers
        if len(racers) != sum(row['ready'] for row in racers):
            raise NotReady
        await self.db.execute("""
            UPDATE races SET started=$2 WHERE hash=$1
        """, record['hash'], time.time())
        await ctx.send('Countdown started!')
        for i in range(5, 0, -1):
            await ctx.send(f'{i}...')
            await asyncio.sleep(1)
        await ctx.send('GO!!!')
        await self.db.execute("""
            UPDATE races SET started=$2 WHERE hash=$1
        """, record['hash'], time.time())
        await ctx.send(f'The race has started.\n'
                       f'To declare yourself done and get your official time, type {ctx.prefix}{self.done}.\n'
                       f'To forfeit, type {ctx.prefix}{self.forfeit}.')

    async def end_race(self, ctx: commands.Context, code=None):
        archive = ctx.guild.get_channel((await self._get_guild_config(ctx))['archive'])
        record = await self._get_race_settings(ctx, code)
        channel = ctx.guild.get_channel(record['channel'])
        await channel.edit(category=archive, overwrites={})
        role = ctx.guild.get_role(record['role'])
        await role.delete()
        if record['voicechan'] is not None:
            voicechan = ctx.guild.get_channel(record['voicechan'])
            await voicechan.edit(category=archive)
        await self.db.execute("""
            DELETE FROM racers WHERE hash = $1 
        """, code)

    async def handle_race_finished(self, ctx: commands.Context, code):
        if all(row['finished'] is not None for row in await self._get_racers(ctx, code)):
            await ctx.send('The race has finished. The channel will now be archived.')
            await self.end_race(ctx, code)

    @commands.command()
    @is_host
    async def cancel(self, ctx: commands.Context):
        await ctx.send('The race has been canceled. The channel will now be archived.')
        await self.end_race(ctx)

    @commands.command()
    @commands.check(guild_has_category)
    @commands.check(is_racing)
    @commands.bot_has_permissions(manage_roles=True, manage_channels=True)
    async def done(self, ctx: commands.Context):
        now = time.time()
        race = await self._get_race_settings(ctx)
        start_time = race['started']
        duration = datetime.timedelta(seconds=now - start_time)
        await self.db.execute("""
            UPDATE racers SET finished=$2 WHERE hash = $1 AND id = $3
        """, race['hash'], now, ctx.author.id)
        await ctx.send(f'{ctx.author.mention} has finished the race with an official time of {duration}')
        await self.handle_race_finished(ctx, race['hash'])

    @commands.command()
    @commands.check(guild_has_category)
    @commands.check(is_racing)
    @commands.bot_has_permissions(manage_roles=True, manage_channels=True)
    async def forfeit(self, ctx: commands.Context):
        race = await self._get_race_settings(ctx)
        start_time = race['started']
        await self.db.execute("""
            UPDATE racers SET finished=$2 WHERE hash = $1 AND id = $3
        """, race['hash'], start_time + 18000, ctx.author.id)
        await ctx.send(f'{ctx.author.mention} has forfeited from the race.')
        await self.handle_race_finished(ctx, race['hash'])

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, NoGuildConfig):
            await ctx.send(f'This server is not configured. Please run {ctx.prefix}{self.config}.', delete_after=10)
        elif isinstance(error, GuildConfigExists):
            await ctx.send('This server already has a race category.', delete_after=10)
        elif isinstance(error, RaceNotStarted):
            await ctx.send('You cannot use this command before the race has begun.', delete_after=10)
        elif isinstance(error, RaceDoesNotExist):
            await ctx.send('The indicated race does not exist, or you are using this command outside a race channel.', delete_after=10)
        elif isinstance(error, NotHost):
            await ctx.send('Only the race host may start the race.', delete_after=10)
        elif isinstance(error, NotRacing):
            await ctx.send('You are not a participant in this race, or you have already finished or forfeited.', delete_after=10)
        elif isinstance(error, RaceAlreadyStarted):
            await ctx.send('This race cannot be started more than once.', delete_after=10)
        elif isinstance(error, NotEnoughRacers):
            await ctx.send('Need at least two racers to start a race.', delete_after=10)
        elif isinstance(error, NotReady):
            await ctx.send('All racers must indicate "ready" before you can start', delete_after=10)
        else:
            await ctx.send(f'Unhandled {error.__class__.__name__} in {ctx.command}: {error}', delete_after=10)


def setup(bot):
    bot.add_cog(Race(bot))
