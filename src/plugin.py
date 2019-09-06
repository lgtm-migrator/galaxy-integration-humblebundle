import sys
import asyncio
import logging
import re
import webbrowser
import pathlib
import json
from dataclasses import astuple
from functools import partial
from typing import Any

sys.path.insert(0, str(pathlib.PurePath(__file__).parent / 'modules'))

import sentry_sdk
from galaxy.api.plugin import Plugin, create_and_run_plugin
from galaxy.api.consts import Platform
from galaxy.api.types import Authentication, NextStep, LocalGame
from galaxy.api.errors import AuthenticationRequired

from version import __version__
from settings import Settings
from webservice import AuthorizedHumbleAPI
from model.game import TroveGame, Key, Subproduct
from humbledownloader import HumbleDownloadResolver
from library import LibraryResolver, Strategy
from local import AppFinder


sentry_sdk.init(
    "https://5b8ef07071c74c0a949169c1a8d41d1c@sentry.io/1514964",
    release=f"galaxy-integration-humblebundle@{__version__}"
)


def report_problem(error, extra=None, level=logging.ERROR):
    logging.log(level, repr(error))
    with sentry_sdk.configure_scope() as scope:
        scope.set_extra("extra_context", extra)
        sentry_sdk.capture_exception(error)


AUTH_PARAMS = {
    "window_title": "Login to HumbleBundle",
    "window_width": 560,
    "window_height": 610,
    "start_uri": "https://www.humblebundle.com/login?goto=/home/library",
    # or https://www.humblebundle.com/account-start?goto=home"
    "end_uri_regex": "^" + re.escape("https://www.humblebundle.com/home/library")
}


class HumbleBundlePlugin(Plugin):
    def __init__(self, reader, writer, token):
        super().__init__(Platform.HumbleBundle, __version__, reader, writer, token)
        self._api = AuthorizedHumbleAPI()
        self._download_resolver = HumbleDownloadResolver()
        self._app_finder = AppFinder
        self._settings = None
        self._library_resolver = None

        self._owned_games = {}
        self._local_games = {}
        self._cached_game_states = {}

        self._getting_owned_games = asyncio.Event()
        self._check_owned_task = asyncio.create_task(asyncio.sleep(0))
        self._check_installed_task = asyncio.create_task(asyncio.sleep(5))
        self._check_statuses_task = asyncio.create_task(asyncio.sleep(2))

        self.__under_instalation = set()

    def _save_cache(self, key: str, data: Any):
        if type(data) != str:
            data = json.dumps(data)
        self.persistent_cache[key] = data
        self.push_cache()

    def handshake_complete(self):
        self._settings = Settings(
            cached_version=self.persistent_cache.get('version'),
            cached_config=self.persistent_cache.get('config', ''),
            save_cache_callback=self._save_cache
        )
        self._library_resolver = LibraryResolver(
            api=self._api,
            settings=self._settings.owned,
            cache=json.loads(self.persistent_cache.get('library', '{}')),
            save_cache_callback=partial(self._save_cache, 'library')
        )

    async def authenticate(self, stored_credentials=None):
        if not stored_credentials:
            return NextStep("web_session", AUTH_PARAMS)

        logging.info('stored credentials found')
        user_id, user_name = await self._api.authenticate(stored_credentials)
        return Authentication(user_id, user_name)

    async def pass_login_credentials(self, step, credentials, cookies):
        auth_cookie = next(filter(lambda c: c['name'] == '_simpleauth_sess', cookies))

        user_id, user_name = await self._api.authenticate(auth_cookie)
        self.store_credentials(auth_cookie)
        return Authentication(user_id, user_name)

    async def get_owned_games(self):
        self._getting_owned_games.set()
        self._owned_games = await self._library_resolver(Strategy.FETCH)
        self._getting_owned_games.clear()
        return [g.in_galaxy_format() for g in self._owned_games.values()]

    async def install_game(self, game_id):
        if game_id in self.__under_instalation:
            return
        self.__under_instalation.add(game_id)

        try:
            game = self._owned_games.get(game_id)
            if game is None:
                raise RuntimeError(f'Install game: game {game_id} not found. Owned games: {self._owned_games.keys()}')

            if isinstance(game, Key):
                args = [str(pathlib.Path(__file__).parent / 'keysgui.py'),
                    game.human_name, game.key_type_human_name, str(game.key_val)
                ]
                process = await asyncio.create_subprocess_exec(sys.executable, *args,
                    stderr=asyncio.subprocess.PIPE)
                _, stderr_data = await process.communicate()
                if stderr_data:
                    logging.debug(args)
                    logging.debug(stderr_data)
                return

            chosen_download = self._download_resolver(game)
            if isinstance(game, Subproduct):
                webbrowser.open(chosen_download.web)

            if isinstance(game, TroveGame):
                try:
                    url = await self._api.get_trove_sign_url(chosen_download, game.machine_name)
                except AuthenticationRequired:
                    logging.info('Looks like your Humble Monthly subscription has expired. Refer to config.ini to manage showed games.')
                    webbrowser.open('https://www.humblebundle.com/monthly/subscriber')
                else:
                    webbrowser.open(url['signed_url'])

        except Exception as e:
            report_problem(e, extra=game)
            logging.exception(e)
        finally:
            self.__under_instalation.remove(game_id)

    async def get_local_games(self):
        if not self._app_finder or not self._owned_games:
            return []

        try:
            self._app_finder.refresh()
        except Exception as e:
            report_problem(e, None)
            return []

        local_games = await self._app_finder.find_local_games(list(self._owned_games.values()))
        self._local_games.update({game.machine_name: game for game in local_games})

        return [g.in_galaxy_format() for g in self._local_games.values()]

    async def launch_game(self, game_id):
        try:
            game = self._local_games[game_id]
        except KeyError as e:
            report_problem(e, {'local_games': self._local_games})
        else:
            game.run()

    async def uninstall_game(self, game_id):
        try:
            game = self._local_games[game_id]
        except KeyError as e:
            report_problem(e, {'local_games': self._local_games})
        else:
            game.uninstall()

    async def _check_owned(self):
        """ self.get_owned_games is called periodically by galaxy too rarely.
        This method check for new orders more often and also when relevant option in config file was changed.
        """
        # - cache all request responses on get_owned_games: orderlist && orders && trove && was_trove_subscriber
        # - on get_owned_games: refresh all; **use parsistent_cache; add 1week time counter after which run with mode 'reset'; otherwise run with 'optimized'
        #   filtered_owned_games = resolver(mode='reset')
        # - on changed config.owned: use cache.
        #   filtered_owned_games = resolver(mode='cache')
        # - on periodical check: refresh only things that may change: 
        #       - **was_trove_subscriber if was previously False; if true:
        #         - troves from last seen page + **(& go net only if len(lastpage) == 20) - this on webservice site,
        #       - orderlist; if new orders:
        #         - lacking orders,
        #       - all unrevealed keys orders to check if they has been revealed
        #   filtered_owned_games = resolver(mode='optimized', owned_games)
        # ** - optional; nice to have to maybe it is better to KISS than this optimization
        #
        # LibraryResolver.__init__(webservice, persistent_cache: dict, settings: dict, save_cache_callback)
        # LibraryResolver.__call__(mode: CacheStrategy, settings: dict) -> Dict[str, HumbleGame]  # deduplicated_owned_games
        #   
        old_settings = astuple(self._settings.owned)
        self._settings.reload_local_config_if_changed()
        if old_settings != astuple(self._settings.owned):
            logging.info(f'Library settings has changed: {self._settings.owned}')
            old_ids = self._owned_games.keys()
            self._owned_games = await self._library_resolver(Strategy.CACHE)

            for old_id in old_ids - self._owned_games.keys():
                self.remove_game(old_id)
            for new_id in self._owned_games.keys() - old_ids:
                self.add_game(self._owned_games[new_id].in_galaxy_format())


    async def _check_statuses(self):
        """Check satuses of already found installed (local) games.
        Detects events when game is:
        - launched (via Galaxy for now)
        - stopped
        - uninstalled
        """
        freezed_locals = list(self._local_games.values())
        for game in freezed_locals:
            state = game.state
            if state == self._cached_game_states.get(game.id):
                continue
            self.update_local_game_status(LocalGame(game.id, state))
            self._cached_game_states[game.id] = state
        await asyncio.sleep(0.5)

    async def _check_installed(self):
        """Searches for installed games and updates self._local_games"""
        await self.get_local_games()
        await asyncio.sleep(5)

    def tick(self):
        if self._check_owned_task.done() and not self._getting_owned_games.is_set():
            self._check_owned_task = asyncio.create_task(self._check_owned())

        if self._check_statuses_task.done():
            self._check_statuses_task = asyncio.create_task(self._check_statuses())

        if self._check_installed_task.done():
            self._check_installed_task = asyncio.create_task(self._check_installed())


    def shutdown(self):
        asyncio.create_task(self._api.close_session())


def main():
    create_and_run_plugin(HumbleBundlePlugin, sys.argv)

if __name__ == "__main__":
    main()

