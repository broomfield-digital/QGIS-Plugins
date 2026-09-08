"""Guard the two modules that decide where things are kept.

``settings`` is boring on purpose, and its one interesting failure is silent:
:class:`QgsSettings` round-trips a boolean as the **string** ``"true"`` on some
backends, so a naive ``bool(value)`` reads ``"false"`` as True and every
preference is permanently on. So ``get_bool`` is asserted with ``assertIs``
against the real singletons, not with ``assertTrue``.

``paths`` exists for one measured trap: **headless
``QgsApplication.qgisSettingsDirPath()`` drops the ``QGIS4`` profile segment**
(measured on this build: the desktop app reports
``.../QGIS/QGIS4/profiles/default/``, this process reports
``.../QGIS/profiles/default/``). Re-deriving the cache directory would
therefore point ``qgis_process`` at a different cache from the dock, and every
command-line run would re-download what the panel already has -- which POWER's
own docs call a reason to block a client. The defence is that the path is
resolved **once** into a persisted setting, so the test that matters is the one
that changes ``qgisSettingsDirPath`` between two calls and demands the same
answer.

Every test snapshots and restores the plugin's whole settings namespace, so
running the suite does not modify the developer's real QGIS profile.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from qgis.core import QgsApplication, QgsSettings

from nasa_power.qgis_bridge import paths, settings
from tests.qgis.qgis_case import QgisTestCase


class SettingsCase(QgisTestCase):
    """A test case that leaves the developer's stored preferences as it found them."""

    def setUp(self) -> None:
        super().setUp()
        self.guard_prefix()

    def guard_prefix(self) -> None:
        """Snapshot everything under the plugin prefix; restore it afterwards."""
        store = QgsSettings()
        store.beginGroup(settings.PREFIX)
        saved = {key: store.value(key) for key in store.allKeys()}
        store.endGroup()

        def restore() -> None:
            store = QgsSettings()
            store.remove(settings.PREFIX)
            store.beginGroup(settings.PREFIX)
            for key, value in saved.items():
                store.setValue(key, value)
            store.endGroup()

        self.addCleanup(restore)

    def guard_key(self, key: str) -> None:
        """Remove ``key`` afterwards. For keys outside the plugin prefix."""
        self.addCleanup(lambda: QgsSettings().remove(key))


class SettingsRoundTripTests(SettingsCase):
    """Values come back as the type they went in as."""

    def test_str_round_trip(self) -> None:
        settings.set_str(settings.KEY_COMMUNITY, "AG")
        self.assertEqual(settings.get_str(settings.KEY_COMMUNITY), "AG")

    def test_str_default_when_absent(self) -> None:
        QgsSettings().remove(settings.KEY_TEMPORAL)
        self.assertEqual(settings.get_str(settings.KEY_TEMPORAL, "daily"), "daily")

    def test_bool_round_trip(self) -> None:
        for value in (True, False):
            with self.subTest(value=value):
                settings.set_bool(settings.KEY_AUTO_STYLE, value)
                read = settings.get_bool(settings.KEY_AUTO_STYLE, not value)
                self.assertIs(read, value)

    def test_get_bool_of_a_stored_string_is_a_real_bool(self) -> None:
        # QgsSettings hands booleans back as the strings "true"/"false" on some
        # backends. bool("false") is True, which would leave every boolean
        # preference silently on and no way to turn one off.
        store = QgsSettings()
        store.setValue(settings.KEY_CONVERT_TO_SI, "false")
        read = settings.get_bool(settings.KEY_CONVERT_TO_SI, True)
        self.assertIsInstance(read, bool)
        self.assertIs(read, False)

        store.setValue(settings.KEY_CONVERT_TO_SI, "true")
        read = settings.get_bool(settings.KEY_CONVERT_TO_SI, False)
        self.assertIsInstance(read, bool)
        self.assertIs(read, True)

    def test_bool_default_when_absent(self) -> None:
        QgsSettings().remove(settings.KEY_ADD_TO_MAP)
        self.assertIs(settings.get_bool(settings.KEY_ADD_TO_MAP, True), True)
        self.assertIs(settings.get_bool(settings.KEY_ADD_TO_MAP, False), False)

    def test_int_round_trip(self) -> None:
        settings.set_int(settings.KEY_MAX_CONCURRENCY, 8)
        read = settings.get_int(settings.KEY_MAX_CONCURRENCY, settings.DEFAULT_MAX_CONCURRENCY)
        self.assertIsInstance(read, int)
        self.assertEqual(read, 8)

    def test_int_falls_back_on_an_unreadable_value(self) -> None:
        # An Options page that raised here would be unopenable after a hand-edit
        # of the ini, so an unparseable value has to read as the default.
        QgsSettings().setValue(settings.KEY_MAX_CONCURRENCY, "not a number")
        self.assertEqual(
            settings.get_int(settings.KEY_MAX_CONCURRENCY, settings.DEFAULT_MAX_CONCURRENCY),
            settings.DEFAULT_MAX_CONCURRENCY,
        )

    def test_list_round_trip(self) -> None:
        values = ["T2M", "ALLSKY_SFC_SW_DWN", "PRECTOTCORR"]
        settings.set_list(settings.KEY_LAST_PARAMETERS, values)
        self.assertEqual(settings.get_list(settings.KEY_LAST_PARAMETERS), values)

    def test_single_element_list_stays_a_list(self) -> None:
        # Measured on this build: the ini backend hands a one-element list back
        # as a real one-element list, so this round trip does NOT exercise the
        # bare-string branch -- mutation-checked, deleting that branch leaves
        # this test green. The branch has its own test below.
        settings.set_list(settings.KEY_LAST_PARAMETERS, ["T2M"])
        self.assertEqual(settings.get_list(settings.KEY_LAST_PARAMETERS), ["T2M"])

    def test_a_list_stored_as_a_bare_string_is_split_not_iterated(self) -> None:
        # The classic QSettings trap, reproduced directly because this backend
        # will not produce it on its own: some backends collapse a one-element
        # list to a bare string, and a few write the whole list as one comma
        # separated value. Iterating either yields CHARACTERS -- ["T", "2", "M"]
        # -- so the plugin would ask POWER for parameters named "T", "2" and
        # "M". A hand-edited QGIS.ini reaches this too.
        store = QgsSettings()
        store.setValue(settings.KEY_LAST_PARAMETERS, "T2M")
        self.assertEqual(settings.get_list(settings.KEY_LAST_PARAMETERS), ["T2M"])

        store.setValue(settings.KEY_LAST_PARAMETERS, "T2M,RH2M,PS")
        self.assertEqual(
            settings.get_list(settings.KEY_LAST_PARAMETERS), ["T2M", "RH2M", "PS"]
        )

        # And an empty string is no parameters, not one empty parameter: an ""
        # in the list becomes `parameters=` in the URL and a 422 from POWER.
        store.setValue(settings.KEY_LAST_PARAMETERS, "")
        self.assertEqual(settings.get_list(settings.KEY_LAST_PARAMETERS), [])

    def test_empty_and_absent_lists_are_both_empty(self) -> None:
        settings.set_list(settings.KEY_LAST_PARAMETERS, [])
        self.assertEqual(settings.get_list(settings.KEY_LAST_PARAMETERS), [])
        QgsSettings().remove(settings.KEY_LAST_PARAMETERS)
        self.assertEqual(settings.get_list(settings.KEY_LAST_PARAMETERS), [])


class ResetTests(SettingsCase):
    """``reset_all`` is a complete uninstall of our namespace and nothing else."""

    #: Every key constant the module defines, so a key added later is covered
    #: by these tests without editing them.
    def all_keys(self) -> list[str]:
        return [
            value
            for name, value in vars(settings).items()
            if name.startswith("KEY_") and isinstance(value, str)
        ]

    def test_every_key_lives_under_the_prefix(self) -> None:
        # This is what makes remove(PREFIX) a complete reset: a key defined
        # outside the prefix would survive it and nothing would ever clear it.
        keys = self.all_keys()
        self.assertTrue(keys)
        for key in keys:
            with self.subTest(key=key):
                self.assertTrue(key.startswith(settings.PREFIX + "/"))

    def test_reset_removes_every_stored_key(self) -> None:
        store = QgsSettings()
        for key in self.all_keys():
            store.setValue(key, "sentinel")

        settings.reset_all()

        for key in self.all_keys():
            with self.subTest(key=key):
                self.assertIsNone(store.value(key))

    def test_reset_leaves_other_settings_alone(self) -> None:
        # Including a neighbour whose key merely starts with the same letters:
        # remove() works on groups, so "nasa_power_ui" is not "nasa_power".
        neighbour = f"{settings.PREFIX}_unrelated/keep_me"
        unrelated = "qgis/unrelated_probe_for_nasa_power_tests"
        self.guard_key(neighbour)
        self.guard_key(unrelated)

        store = QgsSettings()
        store.setValue(neighbour, "keep")
        store.setValue(unrelated, "keep")
        settings.set_str(settings.KEY_COMMUNITY, "AG")

        settings.reset_all()

        self.assertEqual(store.value(neighbour), "keep")
        self.assertEqual(store.value(unrelated), "keep")
        self.assertEqual(settings.get_str(settings.KEY_COMMUNITY), "")


class CacheDirResolutionTests(SettingsCase):
    """The cache path is resolved once and then read back, never re-derived."""

    def setUp(self) -> None:
        super().setUp()
        QgsSettings().remove(settings.KEY_CACHE_DIR)

    def profile(self, root: str):
        """Pretend the QGIS profile lives at ``root`` for the duration."""
        return mock.patch.object(
            QgsApplication,
            "qgisSettingsDirPath",
            staticmethod(lambda: root + os.sep),
        )

    def test_headless_settings_dir_omits_the_profile_segment(self) -> None:
        # Measured on QGIS-final-4_2_2.app: this process reports
        # '.../Library/Application Support/QGIS/profiles/default/', while the
        # desktop app reports '.../QGIS/QGIS4/profiles/default/'. That gap is
        # the entire reason the resolved path is persisted rather than derived.
        headless = QgsApplication.qgisSettingsDirPath()
        self.assertTrue(headless)
        self.assertNotIn("QGIS4", headless)

    def test_the_cache_subdirectory_is_pinned(self) -> None:
        # Pinned as a literal, once. Every other assertion in this class builds
        # its expectation from paths.CACHE_SUBDIR, so it agrees with whatever
        # the constant says -- mutation-checked, changing it to cache/power left
        # them all green. The name is load-bearing: the dock and qgis_process
        # have to land on the same directory, and the layout is deliberately
        # the one DAVINCI's ~/.cache/davinci/power scheme can share.
        self.assertEqual(paths.CACHE_SUBDIR, os.path.join("cache", "nasa_power"))

    def test_the_fallback_root_is_absolute(self) -> None:
        # qgisSettingsDirPath() is empty in a bare QgsApplication that has not
        # run initQgis(). Without the guard, Path("") / CACHE_SUBDIR is a
        # RELATIVE path, so the cache would be created in the process's working
        # directory -- for qgis_process, wherever the user happened to run it,
        # a different place on every invocation and never a cache hit.
        with mock.patch.object(
            QgsApplication, "qgisSettingsDirPath", staticmethod(lambda: "")
        ):
            fallback = paths.default_cache_dir()
        self.assertTrue(fallback.is_absolute())
        self.assertEqual(fallback.name, "nasa_power")

    def test_default_sits_under_the_profile(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with self.profile(root):
                self.assertEqual(
                    paths.default_cache_dir(), Path(root) / paths.CACHE_SUBDIR
                )

    def test_first_resolution_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with self.profile(root):
                resolved = paths.resolve_cache_dir()
            self.assertEqual(resolved, Path(root) / paths.CACHE_SUBDIR)
            self.assertTrue(resolved.is_dir())
            self.assertEqual(settings.get_str(settings.KEY_CACHE_DIR), str(resolved))

    def test_second_call_ignores_a_changed_settings_dir(self) -> None:
        # The test that actually proves the trap is handled: between the two
        # calls the profile moves, exactly as it does between the dock and
        # qgis_process. A re-derived path would follow it and split the cache.
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            with self.profile(first):
                resolved = paths.resolve_cache_dir()
            with self.profile(second):
                again = paths.resolve_cache_dir()

            self.assertEqual(again, resolved)
            self.assertNotIn(second, str(again))
            self.assertEqual(settings.get_str(settings.KEY_CACHE_DIR), str(resolved))

    def test_stored_path_is_expanded_but_not_created(self) -> None:
        # HOME is redirected for the duration. Both halves of this test need
        # that: `~` is expanded against the real home otherwise, so the
        # assertion below is about a directory in the developer's own account
        # -- and the moment create=False regresses, the run leaves that
        # directory behind and every later run of this test fails on the
        # leftover rather than on the defect. Measured: exactly that happened.
        home = Path(tempfile.mkdtemp(prefix="power-home-"))
        self.addCleanup(shutil.rmtree, home, True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)

        settings.set_str(settings.KEY_CACHE_DIR, os.path.join("~", "no_such_power_cache"))
        resolved = paths.resolve_cache_dir(create=False)
        self.assertNotIn("~", str(resolved))
        self.assertEqual(resolved, home / "no_such_power_cache")
        self.assertFalse(resolved.exists())


class CacheDirChangeTests(SettingsCase):
    """``set_cache_dir`` moves the pointer, not the files."""

    def test_creates_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "somewhere" / "deeper"
            returned = paths.set_cache_dir(target)

            self.assertEqual(returned, target)
            self.assertTrue(target.is_dir())
            self.assertEqual(settings.get_str(settings.KEY_CACHE_DIR), str(target))
            self.assertEqual(paths.resolve_cache_dir(), target)

    def test_does_not_move_existing_files(self) -> None:
        # Entries are named by a hash of their request URL, so leaving them
        # behind is safe: the old directory is still a valid cache if the user
        # points back at it.
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old"
            old.mkdir()
            entry = old / "a1b2c3.json"
            entry.write_bytes(b"cached")
            paths.set_cache_dir(old)

            new = Path(tmp) / "new"
            paths.set_cache_dir(new)

            self.assertTrue(entry.is_file())
            self.assertEqual(entry.read_bytes(), b"cached")
            self.assertEqual(list(new.iterdir()), [])


class CacheSizeTests(SettingsCase):
    """What the Options page displays."""

    def test_sums_files_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cache"
            (root / "sub").mkdir(parents=True)
            (root / "a.json").write_bytes(b"a" * 100)
            (root / "sub" / "b.nc").write_bytes(b"b" * 23)
            self.assertEqual(paths.cache_size_bytes(root), 123)

    def test_empty_directory_is_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(paths.cache_size_bytes(Path(tmp)), 0)

    def test_missing_directory_is_zero(self) -> None:
        # Before the first fetch there is nothing on disk, and the Options page
        # still has to render a number. Measured: Path.rglob on a missing
        # directory yields nothing rather than raising, so the exists() guard
        # in cache_size_bytes is belt-and-braces and deleting it leaves this
        # green -- what is pinned here is the returned 0, not the guard.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(paths.cache_size_bytes(Path(tmp) / "never_created"), 0)

    def test_defaults_to_the_configured_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cache"
            paths.set_cache_dir(target)
            (target / "entry.nc").write_bytes(b"x" * 512)
            self.assertEqual(paths.cache_size_bytes(), 512)


if __name__ == "__main__":
    import unittest

    unittest.main()
