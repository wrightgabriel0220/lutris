import json
import time

from lutris import settings
from lutris.api import read_api_key
from lutris.database.categories import get_all_games_categories, get_categories
from lutris.database.games import add_game, get_games, get_games_where
from lutris.game import Game
from lutris.gui.widgets import NotificationSource
from lutris.util import http
from lutris.util.log import logger

LIBRARY_URL = settings.SITE_URL + "/api/users/library"
LOCAL_LIBRARY_SYNCING = NotificationSource()
LOCAL_LIBRARY_SYNCED = NotificationSource()
LOCAL_LIBRARY_UPDATED = NotificationSource()
_IS_LOCAL_LIBRARY_SYNCING = False


def is_local_library_syncing():
    """True if the library is syncing now; attempting to sync again will do nothing if so."""
    # This provides access to the mutable global _IS_LOCAL_LIBRARY_SYNCING in a safer
    # way; if you just import the global directly you get a copy of its current state at import
    # time which is not very useful.
    return _IS_LOCAL_LIBRARY_SYNCING


class LibrarySyncer:
    def __init__(self):
        self.categories = {r["id"]: r["name"] for r in get_categories()}
        self.games_categories = get_all_games_categories()

    def _get_request(self, since=None):
        credentials = read_api_key()
        if not credentials:
            return
        url = LIBRARY_URL
        if since:
            url += "?since=%s" % since
        return http.Request(
            url,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Token " + credentials["token"],
            },
        )

    def sync_local_library(self, force: bool = False) -> None:
        global _IS_LOCAL_LIBRARY_SYNCING

        if _IS_LOCAL_LIBRARY_SYNCING:
            return

        if not force and settings.read_setting("last_library_sync_at"):
            since = int(settings.read_setting("last_library_sync_at"))
        else:
            since = None
        all_games = get_games()
        local_library = self._db_games_to_api(all_games)
        local_library_updates = self._db_games_to_api(all_games, since=since)

        request = self._get_request(since)
        if not request:
            return

        LOCAL_LIBRARY_SYNCING.fire()
        any_local_changes = False
        try:
            _IS_LOCAL_LIBRARY_SYNCING = True
            try:
                request.post(data=json.dumps(local_library_updates).encode())
            except http.HTTPError as ex:
                logger.error("Could not send local library to server: %s", ex)
                return None
            library_keys = set()
            duplicate_keys = set()
            library_map = {}
            library_slugs = set()
            for game in local_library:
                library_key = (
                    game["slug"],
                    game["runner"] or "",
                    game["platform"] or "",
                    game["service"] or "",
                )
                if library_key in library_keys:
                    duplicate_keys.add(library_key)
                library_keys.add(library_key)
                library_map[library_key] = game
                library_slugs.add(game["slug"])

            for remote_game in request.json:
                remote_key = (
                    remote_game["slug"],
                    remote_game["runner"] or "",
                    remote_game["platform"] or "",
                    remote_game["service"] or "",
                )
                if remote_key in duplicate_keys:
                    logger.warning("Duplicate game %s, not syncing.", remote_key)
                    continue
                if remote_key in library_map:
                    changed = False
                    conditions = {"slug": remote_game["slug"]}
                    for cond_key in ("runner", "platform", "service"):
                        if remote_game[cond_key]:
                            conditions[cond_key] = remote_game[cond_key]
                    pga_game = get_games_where(**conditions)
                    if len(pga_game) == 0:
                        logger.error("No game found for %s", remote_key)
                        continue
                    if len(pga_game) > 1:
                        logger.error("More than one game found for %s", remote_key)
                        continue
                    pga_game = pga_game[0]
                    game = Game(pga_game["id"])
                    if remote_game["playtime"] > game.playtime:
                        game.playtime = remote_game["playtime"]
                        changed = True
                    if remote_game["lastplayed"] > game.lastplayed:
                        game.lastplayed = remote_game["lastplayed"]
                        changed = True
                    if changed:
                        any_local_changes = True
                        game.save()
                else:
                    if remote_game["slug"] in library_slugs:
                        continue
                    logger.info("Create %s", remote_key)
                    any_local_changes = True
                    add_game(
                        name=remote_game["name"],
                        slug=remote_game["slug"],
                        runner=remote_game["runner"],
                        platform=remote_game["platform"],
                        lastplayed=remote_game["lastplayed"],
                        playtime=remote_game["playtime"],
                        service=remote_game["service"],
                        service_id=remote_game["service_id"],
                        installed=0,
                    )
            settings.write_setting("last_library_sync_at", int(time.time()))
        finally:
            _IS_LOCAL_LIBRARY_SYNCING = False
            LOCAL_LIBRARY_SYNCED.fire()
            if any_local_changes:
                LOCAL_LIBRARY_UPDATED.fire()

    def _db_game_to_api(self, db_game):
        categories = [self.categories[cat_id] for cat_id in self.games_categories.get(db_game["id"], [])]
        return {
            "name": db_game["name"],
            "slug": db_game["slug"],
            "runner": db_game["runner"] or "",
            "platform": db_game["platform"] or "",
            "playtime": "%0.5f" % (db_game["playtime"] or 0),
            "lastplayed": db_game["lastplayed"] or 0,
            "service": db_game["service"] or "",
            "service_id": db_game["service_id"] or "",
            "categories": categories,
        }

    def _db_games_to_api(self, db_games, since=None):
        payload = []
        for db_game in db_games:
            lastplayed = db_game["lastplayed"] or 0
            installed_at = db_game["installed_at"] or 0
            if since and lastplayed < since and installed_at < since:
                continue
            payload.append(self._db_game_to_api(db_game))
        return payload

    def delete_from_remote_library(self, games):
        request = self._get_request()
        if not request:
            return
        try:
            request.delete(data=json.dumps(self._db_games_to_api(games)).encode())
        except http.HTTPError as ex:
            logger.error(ex)
            return None
        return request.json
