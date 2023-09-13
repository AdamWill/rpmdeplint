# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.


import configparser
import errno
import glob
import logging
import os
import shutil
import tempfile
import time
from os import getenv
from pathlib import Path
from typing import BinaryIO, Dict, Optional

import librepo
import requests
import rpm

logger = logging.getLogger(__name__)
requests_session = requests.Session()


REPO_CACHE_DIR = os.path.join(os.sep, "var", "tmp")
REPO_CACHE_NAME_PREFIX = "rpmdeplint-"


class PackageDownloadError(Exception):
    """
    Raised if a package is being downloaded for further analysis but the download fails.
    """

    pass


class RepoDownloadError(Exception):
    """
    Raised if an error occurs downloading repodata
    """

    pass


def get_yumvars() -> Dict[str, str]:
    # This is not all the yumvars, but hopefully good enough...

    try:
        import dnf.conf
        import dnf.rpm
    except ImportError:
        pass
    else:
        installroot = ""
        subst = dnf.conf.Conf().substitutions
        subst["releasever"] = dnf.rpm.detect_releasever(installroot)
        return subst

    try:
        import rpmUtils
        import yum
        import yum.config
    except ImportError:
        pass
    else:
        return {
            "arch": rpmUtils.arch.getCanonArch(),
            "basearch": rpmUtils.arch.getBaseArch(),
            "releasever": yum.config._getsysver(
                "/", ["system-release(releasever)", "redhat-release"]
            ),
        }

    # Probably not going to work but there's not much else we can do...
    return {
        "arch": "$arch",
        "basearch": "$basearch",
        "releasever": "$releasever",
    }


def substitute_yumvars(s: str, yumvars: Dict[str, str]) -> str:
    for name, value in yumvars.items():
        s = s.replace(f"${name}", value)
    return s


def cache_base_path() -> Path:
    default_cache_home = Path.home() / ".cache"
    return Path(getenv("XDG_CACHE_HOME", default_cache_home)) / "rpmdeplint"


def cache_entry_path(checksum: str) -> Path:
    return cache_base_path() / checksum[:1] / checksum[1:]


def clean_cache():
    expiry_time = time.time() - float(
        os.environ.get("RPMDEPLINT_EXPIRY_SECONDS", "604800")
    )
    if not cache_base_path().is_dir():
        return  # nothing to do
    for subdir in cache_base_path().iterdir():
        # Should be a subdirectory named after the first checksum letter
        if not subdir.is_dir():
            continue
        for entry in subdir.iterdir():
            if not entry.is_file():
                continue
            if entry.stat().st_mtime < expiry_time:
                logger.debug("Purging expired cache file %s", entry)
                entry.unlink()


class Repo:
    """
    Represents a Yum ("repomd") package repository to test dependencies against.
    """

    yum_main_config_path = "/etc/yum.conf"
    yum_repos_config_glob = "/etc/yum.repos.d/*.repo"

    @classmethod
    def from_yum_config(cls):
        """
        Yields Repo instances loaded from the system-wide Yum
        configuration in :file:`/etc/yum.conf` and :file:`/etc/yum.repos.d/`.
        """
        yumvars = get_yumvars()
        config = configparser.RawConfigParser()
        config.read([cls.yum_main_config_path] + glob.glob(cls.yum_repos_config_glob))
        for section in config.sections():
            if section == "main":
                continue
            if config.has_option(section, "enabled") and not config.getboolean(
                section, "enabled"
            ):
                continue
            skip_if_unavailable = False
            if config.has_option(section, "skip_if_unavailable"):
                skip_if_unavailable = config.getboolean(section, "skip_if_unavailable")
            if config.has_option(section, "baseurl"):
                baseurl = substitute_yumvars(config.get(section, "baseurl"), yumvars)
                yield cls(
                    section, baseurl=baseurl, skip_if_unavailable=skip_if_unavailable
                )
            elif config.has_option(section, "metalink"):
                metalink = substitute_yumvars(config.get(section, "metalink"), yumvars)
                yield cls(
                    section, metalink=metalink, skip_if_unavailable=skip_if_unavailable
                )
            elif config.has_option(section, "mirrorlist"):
                mirrorlist = substitute_yumvars(
                    config.get(section, "mirrorlist"), yumvars
                )
                yield cls(
                    section,
                    metalink=mirrorlist,
                    skip_if_unavailable=skip_if_unavailable,
                )
            else:
                raise ValueError(
                    "Yum config section %s has no "
                    "baseurl or metalink or mirrorlist" % section
                )

    def __init__(
        self,
        repo_name: str,
        baseurl: Optional[str] = None,
        metalink: Optional[str] = None,
        skip_if_unavailable: bool = False,
    ):
        """
        :param repo_name: Name of the repository, for example "fedora-updates"
                          (used in problems and error messages)
        :param baseurl: URL or filesystem path to the base of the repository
                        (there should be a repodata subdirectory under this)
        :param metalink: URL to a Metalink file describing mirrors where
                         the repository can be found
        :param skip_if_unavailable: If True, suppress errors downloading
                                    repodata from the repository

        Exactly one of the *baseurl* or *metalink* parameters must be supplied.
        """
        self.name = repo_name
        if not baseurl and not metalink:
            raise RuntimeError("Must specify either baseurl or metalink for repo")
        self.baseurl = baseurl
        self.metalink = metalink
        self.skip_if_unavailable = skip_if_unavailable

    def download_repodata(self):
        clean_cache()
        logger.debug(
            "Loading repodata for %s from %s", self.name, self.baseurl or self.metalink
        )
        self.librepo_handle = h = librepo.Handle()
        r = librepo.Result()
        h.repotype = librepo.LR_YUMREPO
        if self.baseurl:
            h.urls = [self.baseurl]
        if self.metalink:
            h.mirrorlist = self.metalink
        h.setopt(
            librepo.LRO_DESTDIR,
            tempfile.mkdtemp(
                self.name, prefix=REPO_CACHE_NAME_PREFIX, dir=REPO_CACHE_DIR
            ),
        )
        h.setopt(librepo.LRO_INTERRUPTIBLE, True)
        h.setopt(librepo.LRO_YUMDLIST, [])
        if self.baseurl and os.path.isdir(self.baseurl):
            self._download_metadata_result(h, r)
            self._yum_repomd = r.yum_repomd
            self._root_path = self.baseurl
            self.primary = open(self.primary_url, "rb")
            self.filelists = open(self.filelists_url, "rb")
        else:
            self._root_path = h.destdir = tempfile.mkdtemp(
                self.name, prefix=REPO_CACHE_NAME_PREFIX, dir=REPO_CACHE_DIR
            )
            self._download_metadata_result(h, r)
            self._yum_repomd = r.yum_repomd
            self.primary = self._download_repodata_file(
                self.primary_checksum, self.primary_url
            )
            self.filelists = self._download_repodata_file(
                self.filelists_checksum, self.filelists_url
            )

    def _download_metadata_result(self, handle: librepo.Handle, result: librepo.Result):
        try:
            handle.perform(result)
        except librepo.LibrepoException as ex:
            raise RepoDownloadError(
                f"Failed to download repodata for {self!r}: {ex.args[1]}"
            ) from ex

    def _download_repodata_file(self, checksum: str, url: str) -> BinaryIO:
        """
        Each created file in cache becomes immutable, and is referenced in
        the directory tree within XDG_CACHE_HOME as
        $XDG_CACHE_HOME/rpmdeplint/<checksum-first-letter>/<rest-of-checksum>

        Both metadata and the files to be cached are written to a tempdir first
        then renamed to the cache dir atomically to avoid them potentially being
        accessed before written to cache.
        """
        filepath_in_cache: Path = cache_entry_path(checksum)
        try:
            f = open(filepath_in_cache, "rb")
        except OSError as e:
            if e.errno == errno.ENOENT:
                pass  # cache entry does not exist, we will download it
            elif e.errno == errno.EISDIR:
                # This is the original cache directory layout, merged in commit
                # 6f11c3708, although it didn't appear in any released version
                # of rpmdeplint. To be helpful we will fix it up, by just
                # deleting the directory and letting it be replaced by a file.
                shutil.rmtree(filepath_in_cache, ignore_errors=True)
            else:
                raise
        else:
            logger.debug("Using cached file %s for %s", filepath_in_cache, url)
            # Bump the modtime on the cache file we are using,
            # since our cache expiry is LRU based on modtime.
            os.utime(f.fileno())  # Python 3.3+
            return f
        filepath_in_cache.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(dir=filepath_in_cache.parent, text=False)
        logger.debug("Downloading %s to cache temp file %s", url, temp_path)
        try:
            f = os.fdopen(fd, "wb+")
        except Exception:
            os.close(fd)
            raise
        try:
            try:
                response = requests_session.get(url, stream=True)
                response.raise_for_status()
                for chunk in response.raw.stream(decode_content=False):
                    f.write(chunk)
                response.close()
            except OSError as e:
                raise RepoDownloadError(
                    "Failed to download repodata file %s for %r: %s"
                    % (os.path.basename(url), self, e)
                )
            f.flush()
            f.seek(0)
            os.fchmod(f.fileno(), 0o644)
            os.rename(temp_path, filepath_in_cache)
            logger.debug("Using cached file %s for %s", filepath_in_cache, url)
            return f
        except Exception:
            f.close()
            os.unlink(temp_path)
            raise

    def _is_header_complete(self, local_path: str) -> bool:
        """
        Returns `True` if the RPM file `local_path` has complete RPM header.
        """
        try:
            with open(local_path, "rb") as f:
                try:
                    ts = rpm.TransactionSet()
                    ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)

                    # Supress the RPM error message printed to stderr in case
                    # the header is not complete. Set the log verbosity to CRIT
                    # to achieve that. This way the critical errors are still
                    # logged, but the expected "bad header" error is not.
                    rpm.setVerbosity(rpm.RPMLOG_CRIT)
                    ts.hdrFromFdno(f)
                    return True
                except rpm.error:
                    return False
                finally:
                    # Revert back to RPMLOG_ERR.
                    rpm.setVerbosity(rpm.RPMLOG_ERR)
        except FileNotFoundError:
            return False

    def download_package_header(self, location: str, baseurl: str) -> str:
        """
        Downloads the package header, so it can be parsed by `hdrFromFdno`.

        There is no function provided by the Python `rpm` module which would
        return the size of RPM header. This method therefore tries to download
        first N bytes of the RPM file and checks if the header is complete or
        not using the `hdrFromFdno` RPM funtion.

        As the header size can be very different from package to package, it
        tries to download first 100KB and if header is not complete, it
        fallbacks to 1MB and 5MB. If that is not enough, the final fallback
        downloads whole RPM file.

        This strategy still wastes some bandwidth, because we are downloading
        first N bytes repeatedly, but because header of typical RPM fits
        into first 100KB usually and because the RPM data is much bigger than
        what we download repeatedly, it saves a lot of time and bandwidth overall.

        Checksums cannot be checked by this method, because checksums work
        only when complete RPM file is downloaded.
        """
        local_path = os.path.join(self._root_path, os.path.basename(location))
        if self.librepo_handle.local:
            logger.debug("Using package %s from local filesystem directly", local_path)
            return local_path

        # Check if we already downloaded this file and return it if so.
        if self._is_header_complete(local_path):
            logger.debug("Using already downloaded package from %s", local_path)
            return local_path

        logger.debug("Loading package %s from repo %s", location, self.name)
        for byterangeend in [100000, 1000000, 5000000, 0]:
            target = librepo.PackageTarget(
                location,
                base_url=baseurl,
                dest=self._root_path,
                handle=self.librepo_handle,
                byterangestart=0,
                byterangeend=byterangeend,
            )

            if byterangeend:
                logger.debug("Download first %s bytes of %s", byterangeend, location)
            else:
                logger.debug("Download %s", location)
            librepo.download_packages([target])
            if target.err and target.err != "Already downloaded":
                raise PackageDownloadError(
                    f"Failed to download {location} from repo {self.name}: {target.err}"
                )
            if self._is_header_complete(target.local_path):
                break

        logger.debug("Saved as %s", target.local_path)
        return target.local_path

    @property
    def yum_repomd(self):
        return self._yum_repomd

    @property
    def repomd_fn(self) -> str:
        return os.path.join(self._root_path, "repodata", "repomd.xml")

    @property
    def primary_url(self) -> str:
        if not self.baseurl:
            raise RuntimeError("baseurl not specified")
        return os.path.join(self.baseurl, self.yum_repomd["primary"]["location_href"])

    @property
    def primary_checksum(self) -> str:
        return self.yum_repomd["primary"]["checksum"]

    @property
    def filelists_checksum(self) -> str:
        return self.yum_repomd["filelists"]["checksum"]

    @property
    def filelists_url(self) -> str:
        if not self.baseurl:
            raise RuntimeError("baseurl not specified")
        return os.path.join(self.baseurl, self.yum_repomd["filelists"]["location_href"])

    def __repr__(self):
        if self.baseurl:
            return f"{self.__class__.__name__}(repo_name={self.name!r}, baseurl={self.baseurl!r})"
        if self.metalink:
            return f"{self.__class__.__name__}(repo_name={self.name!r}, metalink={self.metalink!r})"
        return f"{self.__class__.__name__}(repo_name={self.name!r})"
