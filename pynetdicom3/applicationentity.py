"""
The main user class, represents a DICOM Application Entity
"""
import gc
from inspect import isclass
import logging
import platform
import select
import socket
from struct import pack
import sys
import time

from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian, \
                        ExplicitVRBigEndian, UID

from pynetdicom3.association import Association
from pynetdicom3.utils import PresentationContext, validate_ae_title

def setup_logger():
    """Setup the logger."""
    logger = logging.getLogger('pynetdicom3')
    handler = logging.StreamHandler()
    logger.setLevel(logging.WARNING)
    formatter = logging.Formatter('%(levelname).1s: %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger

LOGGER = setup_logger()


class ApplicationEntity(object):
    """Represents a DICOM Application Entity (AE).

    An AE may be either a server (Service Class Provider or SCP) or a client
    (Service Class User or SCU).

    SCP
    ---
    To use an AE as an SCP, you need to specify the listen `port` number that
    peer AE SCUs can use to request Associations over, as well as the SOP
    Classes that the SCP supports (`scp_sop_class`). If the SCP is being used
    for anything other than the C-ECHO DIMSE service you also need to implement
    the required callbacks.

    The SCP can then be started using `ApplicationEntity.start()`

    C-STORE SCP Example
    ~~~~~~~~~~~~~~~~~~~
    .. code-block:: python

            from pynetdicom3 import AE, StorageSOPClassList

            # Specify the listen port and which SOP Classes are supported
            ae = AE(port=11112, scp_sop_class=StorageSOPClassList)

            # Define the callback for receiving a C-STORE request
            def on_c_store(dataset):
                # Insert your C-STORE handling code here

                # Must return a valid C-STORE status - 0x0000 is Success
                return 0x0000

            ae.on_c_store = on_c_store

            # Start the SCP
            ae.start()

    SCU
    ---
    To use an AE as an SCU you only need to specify the SOP Classes that the SCU
    supports (`scu_sop_class`) and then call `ApplicationEntity.associate(addr,
    port)` where *addr* and *port* are the TCP/IP address and the listen port
    number of the peer SCP, respectively.

    Once the Association is established you can then request any of the DIMSE-C
    or DIMSE-N services.

    C-ECHO SCU Example
    ~~~~~~~~~~~~~~~~~~
    .. code-block:: python

            from pynetdicom3 import AE, VerificationSOPClass

            # Specify which SOP Classes are supported as an SCU
            ae = AE(scu_sop_class=[VerificationSOPClass])

            # Request an association with a peer SCP
            assoc = ae.associate(addr=192.168.2.1, port=104)

            if assoc.is_established:
                status = assoc.send_c_echo()

                # Release the association
                assoc.Release()

    Attributes
    ----------
    acse_timeout : int or float or None
        The maximum amount of time (in seconds) to wait for association related
        messages. A value of None means no timeout. (default: 60)
    active_associations : list of pynetdicom3.association.Association
        The currently active associations between the local and peer AEs
    address : str
        The local AE's TCP/IP address
    ae_title : str or bytes
        The local AE's title
    client_socket : socket.socket
        The socket used for connections with peer AEs
    dimse_timeout : int or float or None
        The maximum amount of time (in seconds) to wait for DIMSE related
        messages. A value of None means no timeout. (default: None)
    network_timeout : int or float or None
        The maximum amount of time (in seconds) to wait for network messages.
        A value of None means no timeout. (default: None)
    maximum_associations : int
        The maximum number of simultaneous associations (default: 2)
    maximum_pdu_size : int
        The maximum PDU receive size in bytes. A value of 0 means there is no
        maximum size (default: 16382)
    port : int
        The local AE's listen port number when acting as an SCP or connection
        port when acting as an SCU. A value of 0 indicates that the operating
        system should choose the port.
    presentation_contexts_scu : List of pynetdicom3.utils.PresentationContext
        The presentation context list when acting as an SCU (SCU only)
    presentation_contexts_scp : List of pynetdicom3.utils.PresentationContext
        The presentation context list when acting as an SCP (SCP only)
    require_calling_aet : str
        If not empty str, the calling AE title must match `require_calling_aet`
        (SCP only)
    require_called_aet : str
        If not empty str the called AE title must match `required_called_aet`
        (SCP only)
    scu_supported_sop : List of pydicom.uid.UID
        The SOP Classes supported when acting as an SCU (SCU only)
    scp_supported_sop : List of pydicom.uid.UID
        The SOP Classes supported when acting as an SCP (SCP only)
    transfer_syntaxes : List of pydicom.uid.UID
        The supported transfer syntaxes
    """
    # pylint: disable=too-many-instance-attributes,too-many-public-methods
    def __init__(self, ae_title='PYNETDICOM', port=0, scu_sop_class=None,
                 scp_sop_class=None, transfer_syntax=None,
                 bind_addr=''):
        """Create a new Application Entity.

        Parameters
        ----------
        ae_title : str, optional
            The AE title of the Application Entity (default: PYNETDICOM)
        port : int, optional
            The port number to listen for connections on when acting as an SCP
            (default: the first available port)
        bind_addr : str, optional
            The network interface to listen to.
            (default: all availabel network interfaces on the machine)
        scu_sop_class : list of pydicom.uid.UID or list of str or list of
        pynetdicom3.sop_class.ServiceClass subclasses, optional
            List of the supported SOP Class UIDs when running as an SCU.
            Either `scu_sop_class` or `scp_sop_class` must have values
        scp_sop_class : list of pydicom.uid.UID or list of UID strings or list
        of pynetdicom3.sop_class.ServiceClass subclasses, optional
            List of the supported SOP Class UIDs when running as an SCP.
            Either scu_`sop_class` or `scp_sop_class` must have values
        transfer_syntax : list of pydicom.uid.UID or list of str or list of
        pynetdicom3.sop_class.ServiceClass subclasses, optional
            List of supported Transfer Syntax UIDs (default: Explicit VR Little
            Endian, Implicit VR Little Endian, Explicit VR Big Endian)
        """
        self.address = platform.node()
        self.port = port
        self.bind_addr = bind_addr
        self.ae_title = ae_title

        # Avoid dangerous default values
        if transfer_syntax is None:
            transfer_syntax = [ExplicitVRLittleEndian,
                               ImplicitVRLittleEndian,
                               ExplicitVRBigEndian]

        # Make sure that one of scu_sop_class/scp_sop_class is not empty
        if scu_sop_class is None and scp_sop_class is None:
            raise ValueError("No supported SOP Class UIDs supplied during "
                             "ApplicationEntity instantiation")

        self.scu_supported_sop = scu_sop_class or []
        self.scp_supported_sop = scp_sop_class or []

        # The transfer syntax(es) available to the AE
        #   At a minimum this must be ... FIXME
        self.transfer_syntaxes = transfer_syntax

        # The user may require the use of Extended Negotiation items
        self.extended_negotiation = []

        # List of active association objects
        self.active_associations = []

        # Default maximum simultaneous associations
        self.maximum_associations = 2

        # Default maximum PDU receive size (in bytes)
        self.maximum_pdu_size = 16382

        # Default timeouts - None means no timeout
        self.acse_timeout = 60
        self.network_timeout = None
        self.dimse_timeout = None

        # Require Calling/Called AE titles to match if value is non-empty str
        self.require_calling_aet = ''
        self.require_called_aet = ''

        self._build_presentation_contexts()

        self.local_socket = None

        # Used to terminate AE when running as an SCP
        self._quit = False

    def start(self):
        """Start the AE as an SCP.

        When running the AE as an SCP this needs to be called to start the main
        loop, it listens for connections on `local_socket` and if they request
        association starts a new Association thread

        Successful associations get added to `active_associations`
        """
        # If the SCP has no supported SOP Classes then there's no point
        #   running as a server
        if self.scp_supported_sop == []:
            LOGGER.error("AE is running as an SCP but no supported SOP classes "
                         "for use with the SCP have been included during"
                         "ApplicationEntity initialisation or by setting the "
                         "scp_supported_sop attribute")
            raise ValueError("AE is running as an SCP but no SCP SOP classes "
                             "have been supplied.")

        # Bind the local_socket to the specified listen port
        #try:
        self._bind_socket()
        #except OSError:
        #    self._quit = True
        #    self.stop()
        #    return

        no_loops = 0
        while True:
            try:
                # #60: Required so we don't max out the CPU
                time.sleep(0.5)

                if self._quit:
                    break

                # Monitor client_socket for association requests and
                #   appends any associations to self.active_associations
                self._monitor_socket()

                # Delete dead associations
                self.cleanup_associations()

                # Every 50 loops run the garbage collection
                if no_loops % 51 == 0:
                    gc.collect()
                    no_loops = 0

                no_loops += 1

            except KeyboardInterrupt:
                self.stop()

    def _bind_socket(self):
        """Set up and bind the SCP socket.

        AE.start(): Set up and bind the socket. Separated out from start() to
        enable better unit testing
        """
        # The socket to listen for connections on, port is always specified
        self.local_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.local_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.local_socket.bind((self.bind_addr, self.port))
        # Listen for connections made to the socket, the backlog argument
        #   specifies the maximum number of queued connections.
        self.local_socket.listen(1)

    def _build_presentation_contexts(self):
        """Build the presentation context list.

        Build presentation context list to be:
        * sent to remote AE when requesting association
          (presentation_contexts_scu)
        * used to decide whether to accept or reject when remote AE
          requests association (presentation_contexts_scp)

        See PS3.8 Sections 7.1.1.13 and 9.3.2.2
        """
        self.presentation_contexts_scu = []
        self.presentation_contexts_scp = []
        for [pc_output, sop_input] in \
                    [[self.presentation_contexts_scu, self.scu_supported_sop],
                     [self.presentation_contexts_scp, self.scp_supported_sop]]:

            for ii, sop_class in enumerate(sop_input):
                # Must be an odd integer between 1 and 255
                presentation_context_id = ii * 2 + 1
                abstract_syntax = sop_class

                # Add the Presentation Context Definition Item
                # If we have too many Items, warn and skip the rest
                if presentation_context_id < 255:
                    pc_item = PresentationContext(presentation_context_id,
                                                  abstract_syntax,
                                                  self.transfer_syntaxes[:])

                    pc_output.append(pc_item)
                else:
                    LOGGER.warning("More than 126 supported SOP Classes have "
                                   "been supplied to the Application Entity, "
                                   "but the Presentation Context Definition ID "
                                   "can only be an odd integer between 1 and "
                                   "255. The remaining SOP Classes will not be "
                                   "included")
                    break

    def _monitor_socket(self):
        """Monitor the local socket for connections.

        AE.start(): Monitors the local socket to see if anyone tries to connect
        and if so, creates a new association. Separated out from start() to
        enable better unit testing
        """
        # FIXME: this needs to be dealt with properly
        try:
            read_list, _, _ = select.select([self.local_socket], [], [], 0)
        except (socket.error, ValueError):
            return

        # If theres a connection
        if read_list:
            client_socket, _ = self.local_socket.accept()
            client_socket.setsockopt(socket.SOL_SOCKET,
                                     socket.SO_RCVTIMEO,
                                     pack('ll', 10, 0))

            # Create a new Association
            # Association(local_ae, local_socket=None, max_pdu=16382)
            assoc = Association(self,
                                client_socket,
                                max_pdu=self.maximum_pdu_size,
                                acse_timeout=self.acse_timeout,
                                dimse_timeout=self.dimse_timeout)
            assoc.start()
            self.active_associations.append(assoc)

    def cleanup_associations(self):
        """Remove dead associations.

        AE.start(): Removes any dead associations from self.active_associations
        by checking to see if the association thread is still alive. Separated
        out from start() to enable better unit testing
        """
        # We can use threading.enumerate() to list all alive threads
        #   assoc.is_alive() is inherited from threading.thread
        self.active_associations = \
            [assoc for assoc in self.active_associations if assoc.is_alive()]

    def stop(self):
        """Stop the SCP.

        When running as an SCP, calling stop() will kill all associations,
        close the listen socket and quit
        """
        self._quit = True

        for assoc in self.active_associations:
            assoc.kill()

        if self.local_socket:
            self.local_socket.close()

    def quit(self):
        """Stop the SCP."""
        self.stop()
        sys.exit(0)

    def associate(self, addr, port, ae_title='ANY-SCP',
                  max_pdu=16382, ext_neg=None):
        """Attempts to associate with a remote application entity

        When requesting an association the local AE is acting as an SCU. The
        Association thread is returned whether or not the association is
        accepted and should be checked using Association.is_established before
        sending any messages.

        Parameters
        ----------
        addr : str
            The peer AE's TCP/IP address (IPv4)
        port : int
            The peer AE's listen port number
        ae_title : str, optional
            The peer AE's title
        max_pdu : int, optional
            The maximum PDV receive size in bytes to use when negotiating the
            association
        ext_neg : List of UserInformation objects, optional
            Used if extended association negotiation is required

        Returns
        -------
        assoc : pynetdicom3.association.Association
            The Association thread
        """
        if not isinstance(addr, str):
            raise TypeError("ip_address must be a valid IPv4 string")

        if not isinstance(port, int):
            raise TypeError("port must be a valid port number")

        peer_ae = {'AET' : validate_ae_title(ae_title),
                   'Address' : addr,
                   'Port' : port}

        # Associate
        assoc = Association(local_ae=self,
                            peer_ae=peer_ae,
                            acse_timeout=self.acse_timeout,
                            dimse_timeout=self.dimse_timeout,
                            max_pdu=max_pdu,
                            ext_neg=ext_neg)
        assoc.start()

        # Endlessly loops while the Association negotiation is taking place
        while (not assoc.is_established and not assoc.is_rejected and
               not assoc.is_aborted and not assoc.dul._kill_thread):
            # Program loops here endlessly sometimes
            time.sleep(0.1)

        # If the Association was established
        if assoc.is_established:
            self.active_associations.append(assoc)

        return assoc

    def __str__(self):
        """ Prints out the attribute values and status for the AE """
        str_out = "\n"
        str_out += "Application Entity '{0!s}' on {1!s}:{2!s}\n" \
                   .format(self.ae_title, self.address, self.port)

        str_out += "\n"
        str_out += "  Available Transfer Syntax(es):\n"
        for syntax in self.transfer_syntaxes:
            str_out += "\t{0!s}\n".format(syntax)

        str_out += "\n"
        str_out += "  Supported SOP Classes (SCU):\n"
        if len(self.scu_supported_sop) == 0:
            str_out += "\tNone\n"
        for sop_class in self.scu_supported_sop:
            str_out += "\t{0!s}\n".format(sop_class)

        str_out += "\n"
        str_out += "  Supported SOP Classes (SCP):\n"
        if len(self.scp_supported_sop) == 0:
            str_out += "\tNone\n"
        for sop_class in self.scp_supported_sop:
            str_out += "\t{0!s}\n".format(sop_class)

        str_out += "\n"
        str_out += "  ACSE timeout: {0!s} s\n".format(self.acse_timeout)
        str_out += "  DIMSE timeout: {0!s} s\n".format(self.dimse_timeout)
        str_out += "  Network timeout: {0!s} s\n".format(self.network_timeout)

        if self.require_called_aet != '' or self.require_calling_aet != '':
            str_out += "\n"
        if self.require_calling_aet != '':
            str_out += "  Required calling AE title: {0!s}\n" \
                       .format(self.require_calling_aet)
        if self.require_called_aet != '':
            str_out += "  Required called AE title: {0!s}\n" \
                       .format(self.require_called_aet)

        str_out += "\n"

        # Association information
        str_out += '  Association(s): {0!s}/{1!s}\n' \
                   .format(len(self.active_associations),
                           self.maximum_associations)

        for assoc in self.active_associations:
            str_out += '\tPeer: {0!s} on {1!s}:{2!s}\n' \
                       .format(assoc.peer_ae['AET'],
                               assoc.peer_ae['Address'],
                               assoc.peer_ae['Port'])

        return str_out

    @property
    def acse_timeout(self):
        """Get the ACSE timeout."""
        return self._acse_timeout

    @acse_timeout.setter
    def acse_timeout(self, value):
        """Set the ACSE timeout."""
        # pylint: disable=attribute-defined-outside-init
        if value is None:
            self._acse_timeout = None
        elif isinstance(value, (int, float)) and value >= 0:
            self._acse_timeout = value
        else:
            LOGGER.warning("acse_timeout set to 60 seconds")
            self._acse_timeout = 60

        for assoc in self.active_associations:
            assoc.acse_timeout = self.acse_timeout
            assoc.acse.acse_timeout = self.acse_timeout

    @property
    def ae_title(self):
        """Get the AE title."""
        return self._ae_title

    @ae_title.setter
    def ae_title(self, value):
        """Get the AE title."""
        # pylint: disable=attribute-defined-outside-init
        try:
            self._ae_title = validate_ae_title(value)
        except:
            raise

    @property
    def dimse_timeout(self):
        """Get the DIMSE timeout."""
        return self._dimse_timeout

    @dimse_timeout.setter
    def dimse_timeout(self, value):
        """Get the DIMSE timeout."""
        # pylint: disable=attribute-defined-outside-init
        if value is None:
            self._dimse_timeout = None
        elif isinstance(value, (int, float)) and value >= 0:
            self._dimse_timeout = value
        else:
            LOGGER.warning("dimse_timeout set to never expire")
            self._dimse_timeout = None

        for assoc in self.active_associations:
            assoc.dimse_timeout = self.dimse_timeout
            assoc.dimse.dimse_timeout = self.dimse_timeout

    @property
    def network_timeout(self):
        """Get the network timeout."""
        return self._network_timeout

    @network_timeout.setter
    def network_timeout(self, value):
        """Set the network timeout."""
        # pylint: disable=attribute-defined-outside-init
        if value is None:
            self._network_timeout = None
        elif isinstance(value, (int, float)) and value >= 0:
            self._network_timeout = value
        else:
            LOGGER.warning("network_timeout set to never expire")
            self._network_timeout = None

        for assoc in self.active_associations:
            assoc.dul.dul_timeout = self.network_timeout

    @property
    def maximum_associations(self):
        """Get the number of maximum associations."""
        return self._maximum_associations

    @maximum_associations.setter
    def maximum_associations(self, value):
        """Set the number of maximum associations."""
        # pylint: disable=attribute-defined-outside-init
        if isinstance(value, int) and value >= 1:
            self._maximum_associations = value
        else:
            LOGGER.warning("maximum_associations set to 1")
            self._maximum_associations = 1

    @property
    def maximum_pdu_size(self):
        """Get the maximum PDU size."""
        return self._maximum_pdu_size

    @maximum_pdu_size.setter
    def maximum_pdu_size(self, value):
        """Set the maximum PDU size."""
        # pylint: disable=attribute-defined-outside-init
        # Bounds and type checking of the received maximum length of the
        #   variable field of P-DATA-TF PDUs (in bytes)
        #   * Must be numerical, greater than or equal to 0 (0 indicates
        #       no maximum length (PS3.8 Annex D.1.1)
        if value >= 0:
            self._maximum_pdu_size = value
        else:
            LOGGER.warning("maximum_pdu_size set to 16382")

    @property
    def port(self):
        """Get the port number."""
        return self._port

    @port.setter
    def port(self, value):
        """Set the port number."""
        # pylint: disable=attribute-defined-outside-init
        if isinstance(value, int) and value >= 0:
            self._port = value
        else:
            raise ValueError("AE port number must be an integer greater then "
                             "or equal to 0")

    @property
    def require_calling_aet(self):
        """Get the required calling AE title."""
        return self._require_calling_aet

    @require_calling_aet.setter
    def require_calling_aet(self, value):
        """Set the required calling AE title."""
        # pylint: disable=attribute-defined-outside-init
        if len(value) > 16:
            value = value[:16]
        self._require_calling_aet = value.strip()

    @property
    def require_called_aet(self):
        """Get the required called AE title."""
        return self._require_called_aet

    @require_called_aet.setter
    def require_called_aet(self, value):
        """Set the required called AE title."""
        # pylint: disable=attribute-defined-outside-init
        if len(value) > 16:
            value = value[:16]
        self._require_called_aet = value.strip()

    @property
    def scu_supported_sop(self):
        """Set the supported SCU classes."""
        return self._scu_supported_sop

    @scu_supported_sop.setter
    def scu_supported_sop(self, sop_list):
        """Set the AE's supported SCU SOP classes.

        Examples of the `sop_list` items:
        - '1.2.3.4'
        - b'1.2.3.4.5'
        - pydicom.uid.UID('1.2.3')
        - pynetdicom3.sop_class.VerificationSOPClass

        Parameters
        ----------
        sop_list : list of str, bytes, UID, pynetdicom3.sop_class.ServiceClass
            The supported SCU SOP classes.
        """
        # pylint: disable=attribute-defined-outside-init
        self._scu_supported_sop = []

        if not isinstance(sop_list, list):
            raise TypeError("scu_supported_sop must be a list of SOP " \
                               "classes.")

        for sop_class in sop_list:
            if isinstance(sop_class, str):
                sop_uid = UID(sop_class)
            elif isclass(sop_class) and 'UID' in sop_class.__dict__:
                sop_uid = UID(sop_class.UID)
            elif isinstance(sop_class, bytes):
                sop_uid = UID(sop_class.decode('utf-8'))
            else:
                continue

            if not sop_uid.is_valid:
                continue

            self._scu_supported_sop.append(sop_uid)

        if sop_list != [] and self._scu_supported_sop == []:
            raise TypeError("No valid SCU SOP classes were supplied")

    @property
    def scp_supported_sop(self):
        """Get the supported SCP classes."""
        return self._scp_supported_sop

    @scp_supported_sop.setter
    def scp_supported_sop(self, sop_list):
        """Set the AE's supported SCP SOP classes.

        Examples of the `sop_list` items:
        - '1.2.3.4'
        - b'1.2.3.4.5'
        - pydicom.uid.UID('1.2.3')
        - pynetdicom3.sop_class.VerificationSOPClass

        Parameters
        ----------
        sop_list : list of str, bytes, UID, pynetdicom3.sop_class.ServiceClass
            The supported SCP SOP classes.
        """
        # pylint: disable=attribute-defined-outside-init
        self._scp_supported_sop = []
        if not isinstance(sop_list, list):
            raise TypeError("scp_supported_sop must be a list of SOP " \
                               "classes.")

        for sop_class in sop_list:
            if isinstance(sop_class, str):
                sop_uid = UID(sop_class)
            elif isinstance(sop_class, bytes):
                sop_uid = UID(sop_class.decode('utf-8'))
            elif isclass(sop_class):
                if 'UID' in sop_class.__dict__:
                    sop_uid = sop_class.UID
                else:
                    continue
            else:
                continue

            if not sop_uid.is_valid:
                continue

            self._scp_supported_sop.append(sop_uid)

        if sop_list != [] and self._scp_supported_sop == []:
            raise TypeError("No valid SCP SOP classes were supplied")

    @property
    def transfer_syntaxes(self):
        """Get the supported transfer syntaxes."""
        return self._transfer_syntaxes

    @transfer_syntaxes.setter
    def transfer_syntaxes(self, transfer_syntaxes):
        """Set the supported transfer syntaxes."""
        # pylint: disable=attribute-defined-outside-init
        self._transfer_syntaxes = []
        if not isinstance(transfer_syntaxes, list):
            raise ValueError("Transfer syntax must be a list of SOP classes.")

        for syntax in transfer_syntaxes:
            if isinstance(syntax, str):
                sop_uid = UID(syntax)
            elif isinstance(syntax, bytes):
                sop_uid = UID(syntax.decode('utf-8'))
            else:
                raise ValueError("Transfer syntax SOP class must be a "
                                 "UID str, UID bytes or UID.")
            if not sop_uid.is_valid:
                raise ValueError("Transfer syntax contained an "
                                 "invalid UID string")

            if sop_uid.is_transfer_syntax:
                self._transfer_syntaxes.append(sop_uid)
            else:
                LOGGER.warning("Attempted to add a non-transfer syntax "
                               "UID '%s'", syntax)

        if self._transfer_syntaxes == []:
            raise ValueError("Transfer syntax must be a list of SOP "
                             "Classes")


    # Association negotiation callbacks
    def on_user_identity_negotiation(self, user_id_type, primary_field,
                                     secondary_field):
        """Callback for when a peer requests user identity negotiations.

        See PS3.7 Annex D.3.3.7.1

        Experimental and will definitely change

        Parameters
        ----------
        user_id_type : int
            The User Identity Type value (1, 2, 3, 4).
        primary_field : bytes
            The value of the Primary Field
        secondary_field : bytes or None
            The value of the Secondary Field. Will be None unless the
            `user_id_type` is 2

        Returns
        -------
        response : bytes or None
            If `user_id_type` is :
              * 1 or 2, then return b''.
              * 3 then return the Kerberos Server ticket.
              * 4 then return the SAML response.
            If the identity check fails then return None
        """
        raise NotImplementedError


    # High-level DIMSE-C callbacks - user should implement these as required
    def on_c_echo(self):
        """Callback for when a C-ECHO request is received.

        User implementation is not required for the C-ECHO service, but if you
        intend to do so it should be defined prior to calling AE.start()

        Called during by pynetdicom3.sop_class.VerificationServiceClass.SCP()
        after receiving a C-ECHO request and immediately prior to sending the
        response. As the status for a C-ECHO response is always Success no
        return value is required.
        """
        # User implementation of on_c_echo is optional
        pass

    def on_c_store(self, dataset):
        """Callback for when a dataset is received following a C-STORE request.

        Must be defined by the user prior to calling AE.start() and must return
        a valid C-STORE status integer value or the corresponding
        pynetdicom3.sop_class.Status object.

        Parameters
        ----------
        dataset : pydicom.dataset.Dataset
            The DICOM dataset sent in the C-STORE request

        Returns
        -------
        status : pynetdicom3.sop_class.Status or int
            A valid return status for the C-STORE operation (see PS3.4 Annex
            B.2.3), must be one of the following Status objects or the
            corresponding integer value:
                Success status
                    StorageServiceClass.Success
                        Success - 0x0000

                Failure statuses
                    StorageServiceClass.OutOfResources
                        Refused: Out of Resources - 0xA7xx
                    StorageServiceClass.DataSetDoesNotMatchingSOPClassFailure
                        Error: Data Set does not match SOP Class - 0xA9xx
                    StorageServiceClass.CannotUnderstand
                        Error: Cannot understand - 0xCxxx

                Warning statuses
                    StorageServiceClass.CoercionOfDataElements
                        Coercion of Data Elements - 0xB000
                    StorageServiceClass.DataSetDoesNotMatchSOPClassWarning
                        Data Set does not matching SOP Class - 0xB007
                    StorageServiceClass.ElementsDiscarded
                        Elements Discarded - 0xB006

        Raises
        ------
        NotImplementedError
            If the callback has not been implemented by the user
        """
        raise NotImplementedError("User must implement the AE.on_c_store "
                                  "function prior to calling AE.start()")

    def on_c_find(self, dataset):
        """Callback for when a dataset is received following a C-FIND.

        Must be defined by the user prior to calling AE.start() and must return
        a valid pynetdicom3.sop_class.Status object. In addition,the
        AE.on_c_find_cancel() callback must also be defined

        Called by QueryRetrieveFindSOPClass subclasses in SCP()

        Parameters
        ----------
        dataset : pydicom.dataset.Dataset
            The DICOM dataset sent via the C-FIND

        Yields
        ------
        status : pynetdicom3.sop_class.Status or int
            A valid return status for the C-FIND operation (see PS3.4 Annex
            C.4.1.1.4), must be one of the following Status objects or the
            corresponding integer value. A Status of Success (0x0000) will be
            automatically sent once all matches are processing if no Cancel or
            Failure statuses are yielded:
            Failure statuses
                QueryRetrieveFindSOPClass.OutOfResources
                    Refused: Out of Resources - 0xA700
                QueryRetrieveFindSOPClass.IdentifierDoesNotMatchSOPClass
                    Identifier does not match SOP Class - 0xA900
                QueryRetrieveFindSOPClass.UnableToProcess
                    Unable to process - 0xCxxx
            Cancel status
                QueryRetrieveFindSOPClass.MatchingTerminatedDueToCancelRequest
                    Matching terminated due to Cancel request - 0xFE00
            Pending statuses
                QueryRetrieveFindSOPClass.Pending
                    Matches are continuing - Current Match is supplied and
                    any Optional Keys were supported in the same manner as
                    Required Keys - 0xFF00
                QueryRetrieveFindSOPClass.PendingWarning
                    Matches are continuing - Warning that one or more
                    Optional Keys were not supported for existence and/or
                    matching for this Identifier - 0xFF01
        dataset : pydicom.dataset.Dataset or None
            A matching dataset if the status is Pending, None otherwise.
        """
        raise NotImplementedError("User must implement the AE.on_c_find "
                                  "function prior to calling AE.start()")

    def on_c_find_cancel(self):
        """Callback for when a C-FIND-CANCEL is received.

        Returns
        -------
        bool
            True if you want to stop the C-FIND operation, False otherwise.
        """
        raise NotImplementedError("User must implement the "
                                  "AE.on_c_find_cancel function prior to "
                                  "calling AE.start()")

    def on_c_get(self, dataset):
        """Callback for when a dataset is received following a C-STORE.

        Must be defined by the user prior to calling AE.start() and must return
        a valid pynetdicom3.sop_class.Status object. In addition,the
        AE.on_c_get_cancel() callback must also be defined

        Parameters
        ----------
        dataset : pydicom.dataset.Dataset
            The DICOM dataset sent via the C-STORE

        Yields
        ------
        int
            The first yielded value should be the total number of matches, after
            that user should yield a status, dataset pair.
        status : pynetdicom3.sop_class.Status or int
            A valid return status for the C-GET operation (see PS3.4 Annex
            C.4.3.1.4), must be one of the following Status objects or the
            corresponding integer value. A Status of Success (0x0000) will be
            automatically sent once all matches are processing if no Cancel or
            Failure statuses are yielded:
            Failure statuses
                QueryRetrieveGetSOPClass.OutOfResourcesNumberOfMatches
                    Refused: Out of Resources, unable to calculate the number
                    of matches - 0xA701
                QueryRetrieveGetSOPClass.OutOfResourcesUnableToPerform
                    Refused: Out of Resources, unable to perform sub-operations
                    - 0xA702
                QueryRetrieveGetSOPClass.IdentifierDoesNotMatchSOPClass
                    Identifier does not match SOP Class - 0xA900
                QueryRetrieveFindSOPClass.UnableToProcess
                    Unable to process - 0xCxxx
            Cancel status
                QueryRetrieveGetSOPClass.Cancel
                    Sub-operations terminated due to Cancel request - 0xFE00
            Warning status
                QueryRetrieveGetSOPClass.Warning
                    Sub-operations complete, one or more failures or warnings
                    - 0xB000
            Pending status
                QueryRetrieveGetSOPClass.Pending
                    Matches are continuing - Current Match is supplied and
                    any Optional Keys were supported in the same manner as
                    Required Keys - 0xFF00
        dataset : pydicom.dataset.Dataset or None
            A matching dataset if the status is Pending, None otherwise.
        """
        raise NotImplementedError("User must implement the AE.on_c_get "
                                  "function prior to calling AE.start()")

    def on_c_get_cancel(self):
        """Callback for when a C-GET-CANCEL is received."""
        raise NotImplementedError("User must implement the "
                                  "AE.on_c_get_cancel function prior to "
                                  "calling AE.start()")

    def on_c_move(self, dataset, move_aet):
        """Callback for when a dataset is received following a C-STORE.

        Must be defined by the user prior to calling AE.start() and must return
        a valid status. In addition,the AE.on_c_move_cancel() callback must
        also be defined.

        Matching Instances will be sent to the known peer AE with AE title
        `move_aet` over a new association. If `move_aet` is unknown then the
        C-MOVE will fail due to 'Move Destination Unknown'.

        A successful match should return a generator with the first value
        the number of matching Instances, the second value the (addr, port) of
        the move destination and the remaining values the matching Instance
        datasets.

        Parameters
        ----------
        dataset : pydicom.dataset.Dataset
            The DICOM dataset sent via the C-MOVE
        move_aet : bytes
            The destination AE title that matching Instances will be sent to.
            `move_aet` will be a correctly formatted AE title (16 chars,
            with trailing spaces as padding)

        Yields
        ------
        number_matches : int
            The first yield should be the number of matching Instances.
        addr, port : str, int
            The second yield should be the TCP/IP address and port number of the
            destination AE (if known) or None, None if unknown.
        status : pynetdicom3.sop_class.Status or int
            The remaining yields should be a status, dataset pair, where status
            is a valid status:

        dataset : pydicom.dataset.Dataset or None
            If the status is 'Pending' then you can (optionally) return a
            Dataset containing the identifiers or None.
        """
        raise NotImplementedError("User must implement the AE.on_c_move "
                                  "function prior to calling AE.start()")

    def on_c_move_cancel(self):
        """Callback for when a C-MOVE-CANCEL is received."""
        raise NotImplementedError("User must implement the "
                                  "AE.on_c_move_cancel function prior to "
                                  "calling AE.start()")


    # High-level DIMSE-N callbacks - user should implement these as required
    def on_n_event_report(self):
        """Callback for when a N-EVENT-REPORT is received."""
        raise NotImplementedError("User must implement the "
                                  "AE.on_n_event_report function prior to "
                                  "calling AE.start()")

    def on_n_get(self):
        """Callback for when a N-GET is received."""
        raise NotImplementedError("User must implement the "
                                  "AE.on_n_get function prior to calling "
                                  "AE.start()")

    def on_n_set(self):
        """Callback for when a N-SET is received."""
        raise NotImplementedError("User must implement the "
                                  "AE.on_n_set function prior to calling "
                                  "AE.start()")

    def on_n_action(self):
        """Callback for when a N-ACTION is received."""
        raise NotImplementedError("User must implement the "
                                  "AE.on_n_action function prior to calling "
                                  "AE.start()")

    def on_n_create(self):
        """Callback for when a N-CREATE is received."""
        raise NotImplementedError("User must implement the "
                                  "AE.on_n_create function prior to calling "
                                  "AE.start()")

    def on_n_delete(self):
        """Callback for when a N-DELETE is received."""
        raise NotImplementedError("User must implement the "
                                  "AE.on_n_delete function prior to calling "
                                  "AE.start()")


    # Communication related callbacks
    def on_receive_connection(self):
        """Callback for a connection is received."""
        raise NotImplementedError()

    def on_make_connection(self):
        """Callback for a connection is made."""
        raise NotImplementedError()


    # High-level Association related callbacks
    def on_association_requested(self, primitive):
        """Callback for an association is requested."""
        pass

    def on_association_accepted(self, primitive):
        """Callback for when an association is accepted.

        Placeholder for a function callback. Function will be called
        when an association attempt is accepted by either the local or peer AE

        Parameters
        ----------
        primitive
            The A-ASSOCIATE-AC PDU instance received from the peer AE
        """
        pass

    def on_association_rejected(self, primitive):
        """Callback for when an association is rejected.

        Placeholder for a function callback. Function will be called
        when an association attempt is rejected by a peer AE

        Parameters
        ----------
        associate_rq_pdu : pynetdicom3.pdu.A_ASSOCIATE_RJ
            The A-ASSOCIATE-RJ PDU instance received from the peer AE
        """
        pass

    def on_association_released(self, primitive=None):
        """Callback for when an association is released."""
        pass

    def on_association_aborted(self, primitive=None):
        """Callback for when an association is aborted."""
        # FIXME: Need to standardise callback parameters for A-ABORT
        pass
