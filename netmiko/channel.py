from os import path
import socket
import functools
import telnetlib
import paramiko
import serial

from netmiko import log
from netmiko.ssh_exception import (
    NetmikoTimeoutException,
    NetmikoAuthenticationException,
)
from netmiko.netmiko_globals import MAX_BUFFER
from netmiko.utilities import write_bytes


def log_reads(func):
    """Handle both session_log and log of reads."""

    @functools.wraps(func)
    def wrapper_decorator(self):
        output = func(self)
        log.debug(f"read_channel: {output}")
        if self.session_log:
            self.session_log.write(output)
        return output

    return wrapper_decorator


def log_writes(func):
    """Handle both session_log and log of writes."""

    @functools.wraps(func)
    def wrapper_decorator(self, out_data):
        func(self, out_data)
        try:
            log.debug(
                "write_channel: {}".format(
                    write_bytes(out_data, encoding=self.encoding)
                )
            )
            if self.session_log:
                if self.session_log.fin or self.session_log.record_writes:
                    self.session_log.write(out_data)
        except UnicodeDecodeError:
            # Don't log non-ASCII characters; this is null characters and telnet IAC (PY2)
            pass
        return None

    return wrapper_decorator


class Channel:
    def __init__(self, protocol):
        self.protocol = protocol
        self.remote_conn = None


class TelnetChannel(Channel):
    def __init__(self, encoding="ascii", session_log=None):
        self.protocol = "telnet"
        self.remote_conn = None
        self.encoding = encoding
        self.session_log = session_log

    def __repr__(self):
        return "TelnetChannel()"

    def establish_connection(self, width=511, height=1000):
        self.remote_conn = telnetlib.Telnet(
            self.host, port=self.port, timeout=self.timeout
        )
        # FIX - move telnet_login into this class?
        self.telnet_login()

    @log_writes
    def write_channel(self, out_data):
        self.remote_conn.write(write_bytes(out_data, encoding=self.encoding))

    @log_reads
    def read_channel(self):
        """Read all of the available data from the telnet channel."""
        output = self.remote_conn.read_very_eager().decode("utf-8", "ignore")
        # FIX?
        if self.ansi_escape_codes:
            output = self.strip_ansi_escape_codes(output)
        return output


class SSHChannel(Channel):
    def __init__(
        self, ssh_params, ssh_hostkey_args=None, encoding="ascii", session_log=None
    ):
        self.ssh_params = ssh_params
        self.blocking_timeout = ssh_params.pop("blocking_timeout", 20)
        self.keepalive = ssh_params.pop("keepalive", 0)
        if ssh_hostkey_args is None:
            self.ssh_hostkey_args = {}
        else:
            self.ssh_hostkey_args = ssh_hostkey_args
        self.protocol = "ssh"
        self.remote_conn = None
        self.session_log = session_log
        self.encoding = encoding

    def __repr__(self):
        return "SSHChannel(ssh_params)"

    def _build_ssh_client(self):
        """Prepare for Paramiko SSH connection."""
        # Create instance of SSHClient object
        remote_conn_pre = paramiko.SSHClient()

        # Load host_keys for better SSH security
        if self.ssh_hostkey_args.get("system_host_keys"):
            remote_conn_pre.load_system_host_keys()
        if self.ssh_hostkey_args.get("alt_host_keys"):
            alt_key_file = self.ssh_hostkey_args["alt_key_file"]
            if path.isfile(alt_key_file):
                remote_conn_pre.load_host_keys(alt_key_file)

        # Default is to automatically add untrusted hosts (make sure appropriate for your env)
        if not self.ssh_hostkey_args.get("ssh_strict", False):
            key_policy = paramiko.AutoAddPolicy()
        else:
            key_policy = paramiko.RejectPolicy()

        remote_conn_pre.set_missing_host_key_policy(key_policy)
        return remote_conn_pre

    def establish_connection(self, width=511, height=1000):
        self.remote_conn_pre = self._build_ssh_client()

        # initiate SSH connection
        try:
            self.remote_conn_pre.connect(**self.ssh_params)
        except socket.error as conn_error:
            self.paramiko_cleanup()
            msg = f"""TCP connection to device failed.

on causes of this problem are:
ncorrect hostname or IP address.
rong TCP port.
ntermediate firewall blocking access.

ce settings: {self.device_type} {self.host}:{self.port}

"""

            # Handle DNS failures separately
            if "Name or service not known" in str(conn_error):
                msg = (
                    f"DNS failure--the hostname you provided was not resolvable "
                    f"in DNS: {self.host}:{self.port}"
                )

            msg = msg.lstrip()
            raise NetmikoTimeoutException(msg)
        except paramiko.ssh_exception.SSHException as no_session_err:
            self.paramiko_cleanup()
            if "No existing session" in str(no_session_err):
                msg = (
                    "Paramiko: 'No existing session' error: "
                    "try increasing 'conn_timeout' to 10 seconds or larger."
                )
                raise NetmikoTimeoutException(msg)
            else:
                raise
        except paramiko.ssh_exception.AuthenticationException as auth_err:
            self.paramiko_cleanup()
            msg = f"""Authentication to device failed.

on causes of this problem are:
nvalid username and password
ncorrect SSH-key file
onnecting to the wrong device

ce settings: {self.device_type} {self.host}:{self.port}

"""

            msg += self.RETURN + str(auth_err)
            raise NetmikoAuthenticationException(msg)

        # Use invoke_shell to establish an 'interactive session'
        self.remote_conn = self.remote_conn_pre.invoke_shell(
            term="vt100", width=width, height=height
        )

        self.remote_conn.settimeout(self.blocking_timeout)
        if self.keepalive:
            self.remote_conn.transport.set_keepalive(self.keepalive)

    @log_writes
    def write_channel(self, out_data):
        self.remote_conn.sendall(write_bytes(out_data, encoding=self.encoding))

    @log_reads
    def read_channel(self):
        """Read all of the available data from the SSH channel."""
        output = ""
        while True:
            if self.remote_conn.recv_ready():
                outbuf = self.remote_conn.recv(MAX_BUFFER)
                if len(outbuf) == 0:
                    raise EOFError("Channel stream closed by remote device.")
                output += outbuf.decode("utf-8", "ignore")
            else:
                break

        # FIX: Move back to base_connection i.e. this a higher-level function?
        # if self.ansi_escape_codes:
        #    output = self.strip_ansi_escape_codes(output)
        return output


class SerialChannel(Channel):
    def __init__(self, encoding="ascii", session_log=None):
        self.protocol = "serial"
        self.remote_conn = None
        self.encoding = encoding
        self.session_log = session_log

    def __repr__(self):
        return "SerialChannel()"

    def establish_connection(self, width=511, height=1000):
        self.remote_conn = serial.Serial(**self.serial_settings)
        self.serial_login()

    @log_writes
    def write_channel(self, out_data):
        self.remote_conn.write(write_bytes(out_data, encoding=self.encoding))
        self.remote_conn.flush()

    @log_reads
    def read_channel(self):
        """Read all of the available data from the serial channel."""
        output = ""
        while self.remote_conn.in_waiting > 0:
            output += self.remote_conn.read(self.remote_conn.in_waiting).decode(
                "utf-8", "ignore"
            )

        # FIX: Move back to base_connection i.e. this a higher-level function?
        # if self.ansi_escape_codes:
        #    output = self.strip_ansi_escape_codes(output)
        return output
