import argparse
import codecs
import concurrent.futures
import configparser
import importlib
from ipaddress import IPv6Address, AddressValueError
from logging import StreamHandler, Formatter
from logging.handlers import SysLogHandler
import selectors
import socket
from struct import pack
import sys
import os
import re
import signal
import fcntl
import time
import pwd
import grp
import netifaces
import logging
import logging.handlers
import types

import dhcp
from dhcp.ipv6.duids import DUID, LinkLayerDUID
from dhcp.ipv6.handlers import Handler
from dhcp.ipv6.listening_socket import ListeningSocket, ListeningSocketError
from dhcp.ipv6.messages import Message
from dhcp.utils import camelcase_to_dash

logger = logging.getLogger()


class ServerConfigParser(configparser.ConfigParser):
    class SectionNameNormalisingRegEx:
        SECTCRE = configparser.ConfigParser.SECTCRE

        def match(self, value):
            # Do matching using the normal re
            matches = self.SECTCRE.match(value)

            # No match: don't change anything
            if not matches:
                return matches

            # Match! Now monkey-patch the result
            header = matches.group('header')
            header = ServerConfigParser.normalise_section_name(header)

            # And recreate
            return self.SECTCRE.match('[{}]'.format(header))

    SECTCRE = SectionNameNormalisingRegEx()

    def optionxform(self, optionstr: str) -> str:
        """
        Transform option names to a standard form. Allow options with underscores and convert those to dashes.

        :param optionstr: The original option name
        :returns: The normalised option name
        """
        return optionstr.lower().replace('_', '-')

    @staticmethod
    def normalise_section_name(section: str) -> str:
        # Collapse multiple spaces
        section = re.sub(r'[\t ]+', ' ', section)

        # Split
        parts = section.split(' ')
        parts[0] = parts[0].lower()

        # Special section names
        if parts[0] == 'interface':
            # Check name structure
            if len(parts) != 2:
                raise configparser.ParsingError("Interface sections must be named [interface xyz] "
                                                "where 'xyz' is an interface name")

        elif parts[0] == 'option':
            # Check name structure
            if len(parts) != 2:
                raise configparser.ParsingError("Option sections must be named [option xyz] "
                                                "where 'xyz' is an option name")

            if '-' in parts[1] or '_' in parts[1]:
                parts[1] = parts[1].replace('_', '-').lower()
            else:
                parts[1] = camelcase_to_dash(parts[1])

        # Reconstruct
        return ' '.join(parts)

    def add_section(self, section) -> None:
        section = self.normalise_section_name(section)
        super().add_section(section)


def handle_args():
    parser = argparse.ArgumentParser(
        description="A flexible IPv6 DHCP server written in Python.",
    )

    parser.add_argument("config", help="the configuration file")
    parser.add_argument("-C", "--show-config", action="store_true", help="Show the active configuration")
    parser.add_argument("-v", "--verbosity", action="count", default=0, help="increase output verbosity")

    args = parser.parse_args()

    return args


def load_config(config_filename) -> configparser.ConfigParser:
    logger.debug("Loading configuration file {}".format(config_filename))

    config = ServerConfigParser()

    # Create mandatory sections and options
    config.add_section('config')
    config['config']['filename'] = ''

    config.add_section('handler')
    config['handler']['module'] = ''
    config['handler']['class'] = ''

    config.add_section('logging')
    config['logging']['facility'] = 'daemon'

    config.add_section('server')
    config['server']['duid'] = ''
    config['server']['user'] = 'nobody'
    config['server']['group'] = 'nobody'
    config['server']['exception-window'] = '1.0'
    config['server']['max-exceptions'] = '10'
    config['server']['threads'] = '10'

    try:
        config_filename = os.path.realpath(config_filename)
        config_file = open(config_filename, mode='r', encoding='utf-8')
        config.read_file(config_file)
    except FileNotFoundError:
        logger.error("Configuration file {} not found".format(config_filename))
        sys.exit(1)

    # Store the full config file name in the config
    config['config']['filename'] = config_filename

    return config


def set_up_logger(config: configparser.ConfigParser, verbosity: int=0) -> logging.Logger:
    # Don't filter on level in the root logger
    logger.setLevel(logging.NOTSET)

    # Determine syslog facility
    facility_name = config['logging']['facility'].lower()
    facility = logging.handlers.SysLogHandler.facility_names.get(facility_name)
    if not facility:
        logger.critical("Invalid logging facility: {}".format(facility_name))
        sys.exit(1)

    # Create the syslog handler
    syslog_handler = SysLogHandler(facility=facility)
    logger.addHandler(syslog_handler)

    if verbosity > 0:
        # Also output to sys.stdout
        stdout_handler = StreamHandler(stream=sys.stdout)

        # Set level according to verbosity
        if verbosity >= 3:
            stdout_handler.setLevel(logging.DEBUG)
        elif verbosity == 2:
            stdout_handler.setLevel(logging.INFO)
        else:
            stdout_handler.setLevel(logging.WARNING)

        # Set output style according to verbosity
        if verbosity >= 3:
            stdout_handler.setFormatter(Formatter('{asctime} [{threadName}] {name}#{lineno} [{levelname}] {message}',
                                                  style='{'))
        elif verbosity == 2:
            stdout_handler.setFormatter(Formatter('{asctime} [{levelname}] {message}',
                                                  datefmt=Formatter.default_time_format, style='{'))

        logger.addHandler(stdout_handler)


def get_handler(config: configparser.ConfigParser) -> Handler:
    handler_module_name = config['handler'].get('module')
    handler_class_name = config['handler'].get('class') or 'handler'

    if not handler_module_name:
        logger.critical("No handler module configured")
        sys.exit(1)

    logger.info("Importing request handler from {}".format(handler_module_name))

    try:
        handler_module = importlib.import_module(handler_module_name)
    except ImportError as e:
        logger.critical(str(e))
        sys.exit(1)

    try:
        handler = getattr(handler_module, handler_class_name)
        if isinstance(handler, str):
            # Must be the name of the class
            handler = getattr(handler_module, handler, None)

        if callable(handler):
            # It's a method or a class: call it
            handler = handler(config)

        if not isinstance(handler, Handler):
            logger.critical("{}.{}() is not a subclass of dhcp.ipv6.handlers.Handler".format(handler_module_name,
                                                                                             handler_class_name))
            sys.exit(1)
    except (AttributeError, TypeError) as e:
        logger.critical("Cannot initialise handler from module {}: {}".format(handler_module_name, e))
        sys.exit(1)

    return handler


def determine_interface_configs(config: configparser.ConfigParser) -> None:
    """
    Refine the config sections about interfaces. This will expand wildcards, resolve addresses etc.

    :param config: the config parser object
    :return: the list of configured interface names
    """
    interface_names = netifaces.interfaces()

    # Check the interface sections
    for section_name in config.sections():
        parts = section_name.split(' ')

        # Skip non-interface sections
        if parts[0] != 'interface':
            continue

        # Check interface existence
        interface_name = parts[1]
        if interface_name != '*' and interface_name not in interface_names:
            logger.critical("Interface '{}' not found".format(interface_name))
            sys.exit(1)

        section = config[section_name]

        # Add some defaults if necessary
        section.setdefault('multicast', 'no')
        section.setdefault('listen-to-self', 'no')
        if section.getboolean('multicast'):
            # Multicast interfaces need a link-local address
            section.setdefault('link-local-addresses', 'auto')
        else:
            section.setdefault('link-local-addresses', '')
        section.setdefault('global-addresses', '')

        # make sure these values are booleans
        section.getboolean('multicast')
        section.getboolean('listen-to-self')

        # Make sure that these are 'all', 'auto', or a sequence of addresses
        for option_name in ('link-local-addresses', 'global-addresses'):
            option_value = section[option_name].lower().strip()

            if option_value not in ('all', 'auto'):
                option_values = set()
                for addr_str in re.split('[,\t ]+', option_value):
                    if not addr_str:
                        # Empty is ok
                        continue

                    try:
                        addr = IPv6Address(addr_str)

                        if option_name == 'link-local-addresses' and not addr.is_link_local:
                            logger.critical("Interface {} option {} must contain "
                                            "link-local addresses".format(interface_name, option_name))
                            sys.exit(1)

                        if option_name == 'global-addresses' and not (addr.is_global or addr.is_private) \
                                or addr.is_multicast:
                            logger.critical("Interface {} option {} must contain "
                                            "global unicast addresses".format(interface_name, option_name))
                            sys.exit(1)

                        option_values.add(addr)

                    except AddressValueError:
                        logger.critical("Interface {} option {} must contain "
                                        "valid IPv6 addresses".format(interface_name, option_name))
                        sys.exit(1)

                section[option_name] = ' '.join(map(str, option_values))

    # Apply default to unconfigured interfaces
    if config.has_section('interface *'):
        interface_template = config['interface *']

        # Copy from wildcard to other interfaces
        for interface_name in interface_names:
            section_name = 'interface {}'.format(interface_name)
            if config.has_section(section_name):
                # Don't touch it
                pass
            else:
                config.add_section(section_name)
                section = config[section_name]
                for option_name, option_value in interface_template.items():
                    section[option_name] = option_value

        # Forget about the wildcard
        del config['interface *']

    # Expand 'all' and 'auto' and validate the result
    interface_names = [section_name.split(' ')[1] for section_name in config.sections()
                       if section_name.split(' ')[0] == 'interface']
    for interface_name in interface_names:
        section_name = 'interface {}'.format(interface_name)
        section = config[section_name]

        for option_name in ('link-local-addresses', 'global-addresses'):
            option_value = section[option_name].lower()

            if option_value in ('auto', 'all'):
                logger.info("Discovering {} on interface {}".format(option_name, interface_name))

                # Get all addresses
                available_addresses = netifaces.ifaddresses(interface_name).get(netifaces.AF_INET6, [])
                available_addresses = [address_info['addr'] for address_info in available_addresses]
                available_addresses = [IPv6Address(address.split('%')[0]) for address in available_addresses]

                # Filter on type
                if option_name == 'link-local-addresses':
                    available_addresses = [address for address in available_addresses if address.is_link_local]
                elif option_name == 'global-addresses':
                    available_addresses = [address for address in available_addresses
                                           if not address.is_link_local and (address.is_global
                                                                             or address.is_private)]

                for address in available_addresses:
                    logger.debug("- Found {}".format(address))

                if option_value == 'all':
                    logger.debug("= Using all of them")

                elif option_value == 'auto':
                    # Pick the 'best' one if the config says 'auto'
                    # TODO: need to take autoconf/temporary/etc into account once netifaces implements those

                    # First try to find an address with the universal bit set
                    universal_addresses = [address for address in available_addresses if address.packed[8] & 2]
                    if universal_addresses:
                        # Take the lowest universal address
                        available_addresses = [min(universal_addresses)]

                    elif available_addresses:
                        # Take the lowest available address
                        available_addresses = [min(available_addresses)]

                    logger.debug("= Chose {} as 'best' address".format(available_addresses[0]))

                # Store list of addresses as strings. Yes, this means we probably have to parse them again later but I
                # want to keep the config as clean strings.
                section[option_name] = ' '.join(map(str, available_addresses))

        # Remove interfaces without addresses
        if not section['link-local-addresses'] and not section['global-addresses']:
            del config[section_name]
            continue

        # Check that multicast interfaces have a link-local address
        if section.getboolean('multicast') and not section['link-local-addresses']:
            logger.critical("Interface {} listens for multicast requests "
                            "but has no link-local address to reply from".format(interface_name))
            sys.exit(1)


def determine_server_duid(config: configparser.ConfigParser):
    # Try to get the server DUID from the configuration
    config_duid = config['server']['duid']
    if config_duid:
        config_duid = config_duid.strip()
        try:
            duid = bytes.fromhex(config_duid.strip())
        except ValueError:
            logger.critical("Configured hex DUID contains invalid characters")
            sys.exit(1)

        # Check if we can parse this DUID
        length, duid = DUID.parse(duid, length=len(duid))
        if not isinstance(duid, DUID):
            logger.critical("Configured DUID is invalid")
            sys.exit(1)

        logger.info("Using server DUID from configuration: {}", config_duid)

        config['server']['duid'] = codecs.encode(duid.save(), 'hex').decode('ascii')
        return

    # Use the first interface's MAC address as default
    if config:
        interface_names = [section_name.split(' ')[1] for section_name in config.sections()
                           if section_name.split(' ')[0] == 'interface']
        interface_names.sort()

        for interface_name in interface_names:
            link_addresses = netifaces.ifaddresses(interface_name).get(netifaces.AF_LINK, [])
            link_addresses = [link_address['addr'] for link_address in link_addresses if link_address.get('addr')]
            link_addresses.sort()

            for link_address in link_addresses:
                # Try to decode
                try:
                    ll_addr = bytes.fromhex(link_address.replace(':', ''))

                    duid = LinkLayerDUID(hardware_type=1, link_layer_address=ll_addr).save()

                    logger.info("Using server DUID based on {} link address: "
                                "{}".format(interface_name, codecs.encode(duid, 'hex').decode('ascii')))

                    config['server']['duid'] = codecs.encode(duid, 'hex').decode('ascii')
                    return
                except ValueError:
                    # Try the next one
                    pass

    # We didn't find a useful server DUID
    logger.critical("Cannot find a usable DUID")
    sys.exit(1)


def get_sockets(config: configparser.ConfigParser) -> [ListeningSocket]:
    logger.debug("Creating sockets")

    mc_address = dhcp.ipv6.All_DHCP_Relay_Agents_and_Servers
    port = dhcp.ipv6.SERVER_PORT

    # Placeholders for exception message
    interface_name = 'unknown'
    address = 'unknown'

    try:
        sockets = []

        interface_names = [section_name.split(' ')[1] for section_name in config.sections()
                           if section_name.split(' ')[0] == 'interface']
        for interface_name in interface_names:
            section_name = 'interface {}'.format(interface_name)
            section = config[section_name]

            interface_index = socket.if_nametoindex(interface_name)

            for address_str in section['global-addresses'].split(' '):
                if not address_str:
                    continue

                address = IPv6Address(address_str)
                logger.debug("- Creating socket for {} on {}".format(address, interface_name))

                sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                sock.bind((str(address), port))
                sockets.append(ListeningSocket(sock))

            link_local_sockets = []
            for address_str in section['link-local-addresses'].split(' '):
                if not address_str:
                    continue

                address = IPv6Address(address_str)
                logger.debug("- Creating socket for {} on {}".format(address, interface_name))

                sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                sock.bind((str(address), port, 0, interface_index))
                link_local_sockets.append((address, sock))
                sockets.append(ListeningSocket(sock))

            if section.getboolean('multicast'):
                address = mc_address
                reply_from = link_local_sockets[0]

                logger.debug(
                    "- Creating socket for {} with {} as reply-from address on {} ".format(address, reply_from[0],
                                                                                           interface_name))

                sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                sock.bind((address, port, 0, interface_index))

                if section.getboolean('listen-to-self'):
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_LOOP, 1)

                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP,
                                pack('16sI', IPv6Address('ff02::1:2').packed, interface_index))

                sockets.append(ListeningSocket(sock, reply_from[1]))

    except OSError as e:
        logger.critical(
            "Cannot create socket for address {} on interface {}: {}".format(address, interface_name, e.strerror))
        sys.exit(1)
    except ListeningSocketError as e:
        logger.critical(str(e))
        sys.exit(1)

    return sockets


def drop_privileges(uid_name: str, gid_name: str):
    if os.getuid() != 0:
        logger.info("Not running as root: cannot change uid/gid to {}/{}".format(uid_name, gid_name))
        return

    # Get the uid/gid from the name
    running_uid = pwd.getpwnam(uid_name).pw_uid
    running_gid = grp.getgrnam(gid_name).gr_gid

    # Remove group privileges
    os.setgroups([])

    # Try setting the new uid/gid
    os.setgid(running_gid)
    os.setuid(running_uid)

    # Ensure a very conservative umask
    os.umask(0o077)

    logger.info("Dropped privileges to {}/{}".format(uid_name, gid_name))


def create_handler_callback(listening_socket: ListeningSocket, sender: tuple) -> types.FunctionType:
    def callback(future: concurrent.futures.Future):
        try:
            # Get the result
            result = future.result()

            # Allow either None, a Message or a (Message, destination) tuple from the handler
            if result is None:
                # No response: we're done with this request
                return
            elif isinstance(result, Message):
                # Just a message returned, send reply to the sender
                msg_out, destination = result, sender
            elif isinstance(result, tuple):
                # Explicit destination specified, use that
                msg_out, destination = result
            else:
                msg_out = None
                destination = None

            if not isinstance(msg_out, Message) or not isinstance(destination, tuple) or len(destination) != 4:
                logger.error("Handler returned invalid result, not sending a reply to {}".format(destination[0]))
                return

            try:
                pkt_out = msg_out.save()
            except ValueError as e:
                logger.error("Handler returned invalid message: {}".format(e))
                return

            success = listening_socket.send_reply(pkt_out, destination)
            if success:
                logger.debug("Sent {} to {}".format(msg_out.__class__.__name__, destination[0]))
            else:
                logger.error("{} to {} could not be sent".format(msg_out.__class__.__name__, destination[0]))

        except concurrent.futures.CancelledError:
            pass

        except Exception as e:
            # Catch-all exception handler
            logger.exception("Caught unexpected exception {!r}".format(e))

    return callback


def main() -> int:
    args = handle_args()
    config = load_config(args.config)
    set_up_logger(config, args.verbosity)

    logger.info("Starting Python DHCPv6 server v{}".format(dhcp.__version__))

    determine_interface_configs(config)
    determine_server_duid(config)

    if args.show_config:
        config.write(sys.stdout)
        sys.exit(0)

    sockets = get_sockets(config)
    drop_privileges(config['server']['user'], config['server']['group'])

    handler = get_handler(config)

    sel = selectors.DefaultSelector()
    for sock in sockets:
        sel.register(sock, selectors.EVENT_READ)

    # Convert signals to messages on a pipe
    signal_r, signal_w = os.pipe()
    flags = fcntl.fcntl(signal_w, fcntl.F_GETFL, 0)
    flags = flags | os.O_NONBLOCK
    fcntl.fcntl(signal_w, fcntl.F_SETFL, flags)
    signal.set_wakeup_fd(signal_w)
    sel.register(signal_r, selectors.EVENT_READ)

    # Ignore normal signal handling
    signal.signal(signal.SIGINT, lambda signum, frame: None)
    signal.signal(signal.SIGTERM, lambda signum, frame: None)
    signal.signal(signal.SIGHUP, lambda signum, frame: None)

    # Excessive exception catcher
    exception_history = []

    logger.info("Python DHCPv6 server is ready to handle requests")

    exception_window = config['server'].getfloat('exception-window')
    max_exceptions = config['server'].getint('max-exceptions')
    workers = max(1, config['server'].getint('threads'))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        stopping = False
        while not stopping:
            # noinspection PyBroadException
            try:
                events = sel.select()
                for key, mask in events:
                    # Handle signal notifications
                    if key.fileobj == signal_r:
                        signal_nr = os.read(signal_r, 1)
                        if signal_nr[0] in (signal.SIGHUP,):
                            # SIGHUP tells the handler to reload
                            handler.reload()
                        elif signal_nr[0] in (signal.SIGINT, signal.SIGTERM):
                            logger.info("Received termination request")

                            stopping = True
                            break

                        # Unknown signal: ignore
                        continue

                    pkt, sender = key.fileobj.recv_request()
                    try:
                        length, msg_in = Message.parse(pkt)
                    except ValueError as e:
                        logging.info("Invalid message from {}: {}".format(sender[0], str(e)))
                        continue

                    # Submit this request to the worker pool
                    receiver = key.fileobj.listen_socket.getsockname()
                    future = executor.submit(handler.handle, msg_in, sender, receiver)

                    # Create the callback
                    callback = create_handler_callback(key.fileobj, sender)
                    future.add_done_callback(callback)

            except Exception as e:
                # Catch-all exception handler
                logger.exception("Caught unexpected exception {!r}".format(e))

                now = time.monotonic()

                # Add new exception time to the history
                exception_history.append(now)

                # Remove exceptions outside the window from the history
                cutoff = now - exception_window
                while exception_history and exception_history[0] < cutoff:
                    exception_history.pop(0)

                # Did we receive too many exceptions shortly after each other?
                if len(exception_history) > max_exceptions:
                    logger.critical("Received more than {} exceptions in {} seconds, exiting".format(max_exceptions,
                                                                                                     exception_window))
                    stopping = True

    logger.info("Shutting down Python DHCPv6 server v{}".format(dhcp.__version__))

    return 0


def run() -> int:
    try:
        return main()
    except configparser.Error as e:
        logger.critical("Configuration error: {}".format(e))
        sys.exit(1)


if __name__ == '__main__':
    # Run the server
    sys.exit(run())