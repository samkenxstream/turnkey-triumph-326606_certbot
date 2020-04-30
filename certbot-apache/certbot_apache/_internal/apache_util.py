""" Utility functions for certbot-apache plugin """
import binascii
import fnmatch
import hashlib
import logging
import re
import shutil
import struct
import subprocess
import time


from cryptography.hazmat.primitives import hashes  # type: ignore
import pkg_resources

from certbot import crypto_util
from certbot import errors
from certbot import util

from certbot.compat import os


logger = logging.getLogger(__name__)


def get_apache_ocsp_struct(ttl, ocsp_response):
    """Create Apache OCSP response structure to be used in response cache

    :param int ttl: Time-To-Live in seconds
    :param str ocsp_response: OCSP response data

    :returns: Apache OCSP structure
    :rtype: `str`

    """
    ttl = time.time() + ttl
    # As microseconds
    ttl_struct = struct.pack('l', int(ttl*1000000))
    return b'\x01'.join([ttl_struct, ocsp_response])


def certid_sha1_hex(cert_path):
    """Hex representation of certificate SHA1 fingerprint

    :param str cert_path: File path to certificate

    :returns: Hex representation SHA1 fingerprint of certificate
    :rtype: `str`

    """
    sha1_hex = binascii.hexlify(certid_sha1(cert_path))
    return sha1_hex.decode('utf-8')


def certid_sha1(cert_path):
    """SHA1 fingerprint of certificate

    :param str cert_path: File path to certificate

    :returns: SHA1 fingerprint bytestring
    :rtype: `str`

    """
    return cert_sha1_fingerprint(cert_path)


def safe_copy(source, target):
    """Copies a file, while verifying the target integrity
    with the source. Retries twice if the initial
    copy fails.

    :param str source: File path of the source file
    :param str target: File path of the target file

    :raises: .errors.PluginError: If file cannot be
        copied or the target file hash does not match
        with the source file.
    """
    for _ in range(3):
        try:
            shutil.copy2(source, target)
        except IOError as e:
            emsg = "Could not copy {} to {}: {}".format(
                source, target, e
            )
            raise errors.PluginError(emsg)
        time.sleep(1)
        try:
            source_hash = _file_hash(source)
            target_hash = _file_hash(target)
        except IOError:
            continue
        if source_hash == target_hash:
            return
    raise errors.PluginError(
        "Safe copy failed. The file integrity does not match"
    )


def _file_hash(filepath):
    """Helper function for safe_copy that calculates a
    sha-256 hash of file.

    :param str filepath: Path of file to calculate hash for

    :returns: File sha-256 hash
    :rtype: str
    """
    fhash = hashlib.sha256()
    with open(filepath, 'rb') as fh:
        fhash.update(fh.read())
    return fhash.hexdigest()


def get_mod_deps(mod_name):
    """Get known module dependencies.

    .. note:: This does not need to be accurate in order for the client to
        run.  This simply keeps things clean if the user decides to revert
        changes.
    .. warning:: If all deps are not included, it may cause incorrect parsing
        behavior, due to enable_mod's shortcut for updating the parser's
        currently defined modules (`.ApacheParser.add_mod`)
        This would only present a major problem in extremely atypical
        configs that use ifmod for the missing deps.

    """
    deps = {
        "ssl": ["setenvif", "mime"]
    }
    return deps.get(mod_name, [])


def get_file_path(vhost_path):
    """Get file path from augeas_vhost_path.

    Takes in Augeas path and returns the file name

    :param str vhost_path: Augeas virtual host path

    :returns: filename of vhost
    :rtype: str

    """
    if not vhost_path or not vhost_path.startswith("/files/"):
        return None

    return _split_aug_path(vhost_path)[0]


def get_internal_aug_path(vhost_path):
    """Get the Augeas path for a vhost with the file path removed.

    :param str vhost_path: Augeas virtual host path

    :returns: Augeas path to vhost relative to the containing file
    :rtype: str

    """
    return _split_aug_path(vhost_path)[1]


def _split_aug_path(vhost_path):
    """Splits an Augeas path into a file path and an internal path.

    After removing "/files", this function splits vhost_path into the
    file path and the remaining Augeas path.

    :param str vhost_path: Augeas virtual host path

    :returns: file path and internal Augeas path
    :rtype: `tuple` of `str`

    """
    # Strip off /files
    file_path = vhost_path[6:]
    internal_path = []

    # Remove components from the end of file_path until it becomes valid
    while not os.path.exists(file_path):
        file_path, _, internal_path_part = file_path.rpartition("/")
        internal_path.append(internal_path_part)

    return file_path, "/".join(reversed(internal_path))


def parse_define_file(filepath, varname):
    """ Parses Defines from a variable in configuration file

    :param str filepath: Path of file to parse
    :param str varname: Name of the variable

    :returns: Dict of Define:Value pairs
    :rtype: `dict`

    """
    return_vars = {}
    # Get list of words in the variable
    a_opts = util.get_var_from_file(varname, filepath).split()
    for i, v in enumerate(a_opts):
        # Handle Define statements and make sure it has an argument
        if v == "-D" and len(a_opts) >= i+2:
            var_parts = a_opts[i+1].partition("=")
            return_vars[var_parts[0]] = var_parts[2]
        elif len(v) > 2 and v.startswith("-D"):
            # Found var with no whitespace separator
            var_parts = v[2:].partition("=")
            return_vars[var_parts[0]] = var_parts[2]
    return return_vars


def unique_id():
    """ Returns an unique id to be used as a VirtualHost identifier"""
    return binascii.hexlify(os.urandom(16)).decode("utf-8")


def included_in_paths(filepath, paths):
    """
    Returns true if the filepath is included in the list of paths
    that may contain full paths or wildcard paths that need to be
    expanded.

    :param str filepath: Filepath to check
    :params list paths: List of paths to check against

    :returns: True if included
    :rtype: bool
    """

    return any(fnmatch.fnmatch(filepath, path) for path in paths)


def parse_defines(apachectl):
    """
    Gets Defines from httpd process and returns a dictionary of
    the defined variables.

    :param str apachectl: Path to apachectl executable

    :returns: dictionary of defined variables
    :rtype: dict
    """

    variables = {}
    define_cmd = [apachectl, "-t", "-D",
                  "DUMP_RUN_CFG"]
    matches = parse_from_subprocess(define_cmd, r"Define: ([^ \n]*)")
    try:
        matches.remove("DUMP_RUN_CFG")
    except ValueError:
        return {}

    for match in matches:
        if match.count("=") > 1:
            logger.error("Unexpected number of equal signs in "
                         "runtime config dump.")
            raise errors.PluginError(
                "Error parsing Apache runtime variables")
        parts = match.partition("=")
        variables[parts[0]] = parts[2]

    return variables


def parse_includes(apachectl):
    """
    Gets Include directives from httpd process and returns a list of
    their values.

    :param str apachectl: Path to apachectl executable

    :returns: list of found Include directive values
    :rtype: list of str
    """

    inc_cmd = [apachectl, "-t", "-D",
               "DUMP_INCLUDES"]
    return parse_from_subprocess(inc_cmd, r"\(.*\) (.*)")


def parse_modules(apachectl):
    """
    Get loaded modules from httpd process, and return the list
    of loaded module names.

    :param str apachectl: Path to apachectl executable

    :returns: list of found LoadModule module names
    :rtype: list of str
    """

    mod_cmd = [apachectl, "-t", "-D",
               "DUMP_MODULES"]
    return parse_from_subprocess(mod_cmd, r"(.*)_module")


def parse_from_subprocess(command, regexp):
    """Get values from stdout of subprocess command

    :param list command: Command to run
    :param str regexp: Regexp for parsing

    :returns: list parsed from command output
    :rtype: list

    """
    stdout = _get_runtime_cfg(command)
    return re.compile(regexp).findall(stdout)


def _get_runtime_cfg(command):
    """
    Get runtime configuration info.

    :param command: Command to run

    :returns: stdout from command

    """
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True)
        stdout, stderr = proc.communicate()

    except (OSError, ValueError):
        logger.error(
            "Error running command %s for runtime parameters!%s",
            command, os.linesep)
        raise errors.MisconfigurationError(
            "Error accessing loaded Apache parameters: {0}".format(
                command))
    # Small errors that do not impede
    if proc.returncode != 0:
        logger.warning("Error in checking parameter list: %s", stderr)
        raise errors.MisconfigurationError(
            "Apache is unable to check whether or not the module is "
            "loaded because Apache is misconfigured.")

    return stdout


def find_ssl_apache_conf(prefix):
    """
    Find a TLS Apache config file in the dedicated storage.
    :param str prefix: prefix of the TLS Apache config file to find
    :return: the path the TLS Apache config file
    :rtype: str
    """
    return pkg_resources.resource_filename(
        "certbot_apache",
        os.path.join("_internal", "tls_configs", "{0}-options-ssl-apache.conf".format(prefix)))


def cert_sha1_fingerprint(cert_path):
    """Read a certificate by its file path and return its SHA-1 fingerprint.

    :param str cert_path: File path to the x509 certificate file

    :returns: SHA-1 fingerprint of the certificate
    :rtype: bytes
    """

    cert = crypto_util.load_cert(cert_path)
    return cert.fingerprint(hashes.SHA1())
