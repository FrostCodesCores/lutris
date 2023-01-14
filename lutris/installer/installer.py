"""Lutris installer class"""
import json
import os
from gettext import gettext as _

from lutris.config import LutrisConfig, write_game_config
from lutris.database.games import add_or_update, get_game_by_field
from lutris.exceptions import AuthenticationError
from lutris.installer import AUTO_ELF_EXE, AUTO_WIN32_EXE
from lutris.installer.errors import ScriptingError
from lutris.installer.installer_file import InstallerFile
from lutris.installer.legacy import get_game_launcher
from lutris.runners import import_runner
from lutris.services import SERVICES
from lutris.util.game_finder import find_linux_game_executable, find_windows_game_executable
from lutris.util.gog import convert_gog_config_to_lutris, get_gog_config_from_path, get_gog_game_path
from lutris.util.log import logger
from lutris.util.moddb import downloadhelper as moddbhelper


class LutrisInstaller:  # pylint: disable=too-many-instance-attributes
    """Represents a Lutris installer"""

    def __init__(self, installer, interpreter, service, appid):
        self.interpreter = interpreter
        self.installer = installer
        self.is_update = False
        self.version = installer["version"]
        self.slug = installer["slug"]
        self.year = installer.get("year")
        self.runner = installer["runner"]
        self.script = installer.get("script")
        self.game_name = installer["name"]
        self.game_slug = installer["game_slug"]
        self.service = self.get_service(initial=service)
        self.service_appid = self.get_appid(installer, initial=appid)
        self.variables = self.script.get("variables", {})
        self.script_files = [
            InstallerFile(self.game_slug, file_id, file_meta)
            for file_desc in self.script.get("files", [])
            for file_id, file_meta in file_desc.items()
        ]
        self.files = []
        self.requires = self.script.get("requires")
        self.extends = self.script.get("extends")
        self.game_id = self.get_game_id()
        self.is_gog = False
        self.discord_id = installer.get('discord_id')

    def get_service(self, initial=None):
        if initial:
            return initial
        if "steam" in self.runner and "steam" in SERVICES:
            return SERVICES["steam"]()
        version = self.version.lower()
        if "humble" in version and "humblebundle" in SERVICES:
            return SERVICES["humblebundle"]()
        if "gog" in version and "gog" in SERVICES:
            return SERVICES["gog"]()

    def get_appid(self, installer, initial=None):
        if installer.get("is_dlc"):
            return installer.get("dlcid")
        if initial:
            return initial
        if not self.service:
            return
        if self.service.id == "steam":
            return installer.get("steamid") or installer.get("service_id")
        game_config = self.script.get("game", {})
        if self.service.id == "gog":
            return game_config.get("gogid") or installer.get("gogid") or installer.get("service_id")
        if self.service.id == "humblebundle":
            return game_config.get("humbleid") or installer.get("humblestoreid") or installer.get("service_id")

    @property
    def script_pretty(self):
        """Return a pretty print of the script"""
        return json.dumps(self.script, indent=4)

    def get_game_id(self):
        """Return the ID of the game in the local DB if one exists"""
        # If the game is in the library and uninstalled, the first installation
        # updates it
        existing_game = get_game_by_field(self.game_slug, "slug")
        if existing_game and (self.extends or not existing_game["installed"]):
            return existing_game["id"]

    @property
    def creates_game_folder(self):
        """Determines if an install script should create a game folder for the game"""
        if self.requires or self.extends:
            # Game is an extension of an existing game, folder exists
            return False
        if self.runner == "steam":
            # Steam games installs in their steamapps directory
            return False
        if not self.script.get("installer"):
            # No command can affect files
            return False
        if (
                self.script_files
                or self.script.get("game", {}).get("gog")
                or self.script.get("game", {}).get("prefix")
        ):
            return True
        command_names = [list(c.keys())[0] for c in self.script.get("installer", [])]
        if "insert-disc" in command_names:
            return True
        return False

    def get_errors(self):
        """Return potential errors in the script"""
        errors = []
        if not isinstance(self.script, dict):
            errors.append("Script must be a dictionary")
            # Return early since the method assumes a dict
            return errors

        # Check that installers contains all required fields
        for field in ("runner", "game_name", "game_slug"):
            if not hasattr(self, field) or not getattr(self, field):
                errors.append("Missing field '%s'" % field)

        # Check that libretro installers have a core specified
        if self.runner == "libretro":
            if "game" not in self.script or "core" not in self.script["game"]:
                errors.append("Missing libretro core in game section")

        # Check that Steam games have an AppID
        if self.runner == "steam":
            if not self.script.get("game", {}).get("appid"):
                errors.append("Missing appid for Steam game")

        # Check that installers don't contain both 'requires' and 'extends'
        if self.script.get("requires") and self.script.get("extends"):
            errors.append("Scripts can't have both extends and requires")
        return errors

    def get_user_provided_file(self):
        """Return the first user provided file, which is used for game stores"""
        for file in self.script_files:
            if file.url.startswith("N/A"):
                return file.id

        return None

    def prepare_game_files(self, patch_version=None):
        """Gathers necessary files before iterating through them."""
        if not self.script_files:
            return
        if self.service and self.service.online and not self.service.is_connected():
            raise AuthenticationError(_("YOu are not authenticated to %s"), self.service.id)

        installer_file_id = self.get_user_provided_file() if self.service else None

        self.files = [file.copy() for file in self.script_files if file.id != installer_file_id]

        # Run variable substitution on the URLs from the script
        for file in self.files:
            file.set_url(self.interpreter._substitute(file.url))
            if moddbhelper.is_moddb_url(file.url):
                file.set_url(moddbhelper.get_moddb_download_url(file.url))

        if installer_file_id and self.service:
            logger.info("Getting files for %s", installer_file_id)
            if self.service.has_extras:
                logger.info("Adding selected extras to downloads")
                self.service.selected_extras = self.interpreter.extras
            if patch_version:
                # If a patch version is given download the patch files instead of the installer
                installer_files = self.service.get_patch_files(self, installer_file_id)
            else:
                installer_files = self.service.get_installer_files(self, installer_file_id, self.interpreter.extras)

            if installer_files:
                for installer_file in installer_files:
                    self.files.append(installer_file)
            else:
                # Failed to get the service game, put back a user provided file
                logger.debug("Unable to get files from service. Setting %s to manual.", installer_file_id)
                self.files.insert(0, InstallerFile(self.game_slug, installer_file_id, {
                    "url": "N/A: Provider installer file",
                    "filename": ""
                }))

    def _substitute_config(self, script_config):
        """Substitute values such as $GAMEDIR in a config dict."""
        config = {}
        for key in script_config:
            if not isinstance(key, str):
                raise ScriptingError(_("Game config key must be a string"), key)
            value = script_config[key]
            if str(value).lower() == 'true':
                value = True
            if str(value).lower() == 'false':
                value = False
            if key == "launch_configs":
                config[key] = [
                    {k: self.interpreter._substitute(v) for (k, v) in _conf.items()}
                    for _conf in value
                ]
            elif isinstance(value, list):
                config[key] = [self.interpreter._substitute(i) for i in value]
            elif isinstance(value, dict):
                config[key] = {k: self.interpreter._substitute(v) for (k, v) in value.items()}
            elif isinstance(value, bool):
                config[key] = value
            else:
                config[key] = self.interpreter._substitute(value)
        return config

    def get_game_config(self):
        """Return the game configuration"""
        if self.requires:
            # Load the base game config
            required_game = get_game_by_field(self.requires, field="installer_slug")
            if not required_game:
                required_game = get_game_by_field(self.requires, field="slug")
            if not required_game:
                raise ValueError("No game matched '%s' on installer_slug or slug" % self.requires)
            base_config = LutrisConfig(
                runner_slug=self.runner, game_config_id=required_game["configpath"]
            )
            config = base_config.game_level
        else:
            config = {"game": {}}

        # Config update
        if "system" in self.script:
            config["system"] = self._substitute_config(self.script["system"])
        if self.runner in self.script and self.script[self.runner]:
            config[self.runner] = self._substitute_config(self.script[self.runner])
        launcher, launcher_config = self.get_game_launcher_config(self.interpreter.game_files)
        if launcher:
            config["game"][launcher] = launcher_config

        if "game" in self.script:
            try:
                config["game"].update(self.script["game"])
            except ValueError as err:
                raise ScriptingError(_("Invalid 'game' section"), self.script["game"]) from err
            config["game"] = self._substitute_config(config["game"])
            if AUTO_ELF_EXE in config["game"].get("exe", ""):
                config["game"]["exe"] = find_linux_game_executable(self.interpreter.target_path,
                                                                   make_executable=True)
            elif AUTO_WIN32_EXE in config["game"].get("exe", ""):
                config["game"]["exe"] = find_windows_game_executable(self.interpreter.target_path)
        config["name"] = self.game_name
        config["script"] = self.script
        config["variables"] = self.variables
        config["version"] = self.version
        config["requires"] = self.requires
        config["slug"] = self.slug
        config["game_slug"] = self.game_slug
        config["year"] = self.year
        if self.service:
            config["service"] = self.service.id
            config["service_id"] = self.service_appid
        return config

    def save(self):
        """Write the game configuration in the DB and config file"""
        if self.extends:
            logger.info(
                "This is an extension to %s, not creating a new game entry",
                self.extends,
            )
            return self.game_id

        if self.is_gog:
            gog_config = get_gog_config_from_path(self.interpreter.target_path)
            if gog_config:
                gog_game_path = get_gog_game_path(self.interpreter.target_path)
                lutris_config = convert_gog_config_to_lutris(gog_config, gog_game_path)
                self.script["game"].update(lutris_config)

        configpath = write_game_config(self.slug, self.get_game_config())
        runner_inst = import_runner(self.runner)()
        if self.service:
            service_id = self.service.id
        else:
            service_id = None
        self.game_id = add_or_update(
            name=self.game_name,
            runner=self.runner,
            slug=self.game_slug,
            platform=runner_inst.get_platform(),
            directory=self.interpreter.target_path,
            installed=1,
            hidden=0,
            installer_slug=self.slug,
            parent_slug=self.requires,
            year=self.year,
            configpath=configpath,
            service=service_id,
            service_id=self.service_appid,
            id=self.game_id,
            discord_id=self.discord_id,
        )
        return self.game_id

    def get_game_launcher_config(self, game_files):
        """Game options such as exe or main_file can be added at the root of the
        script as a shortcut, this integrates them into the game config properly
        This should be deprecated. Game launchers should go in the game section.
        """
        launcher, launcher_value = get_game_launcher(self.script)
        if isinstance(launcher_value, list):
            launcher_values = []
            for game_file in launcher_value:
                if game_file in game_files:
                    launcher_values.append(game_files[game_file])
                else:
                    launcher_values.append(game_file)
            return launcher, launcher_values
        if launcher_value:
            if launcher_value in game_files:
                launcher_value = game_files[launcher_value]
            elif self.interpreter.target_path and os.path.exists(
                    os.path.join(self.interpreter.target_path, launcher_value)
            ):
                launcher_value = os.path.join(self.interpreter.target_path, launcher_value)
        return launcher, launcher_value
