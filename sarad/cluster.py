"""Module to handle a cluster of SARAD instruments.

All instruments forming a cluster are connected to
the same instrument controller.
SaradCluster is used as singleton."""

import logging
import os
import pickle
import sys
from datetime import datetime
from typing import IO, Any, Dict, Generic, Iterator, List, Optional, Set

import serial.tools.list_ports  # type: ignore
from hashids import Hashids  # type: ignore

from sarad.dacm import DacmInst
from sarad.doseman import DosemanInst
from sarad.radonscout import RscInst
from sarad.sari import SI, SaradInst

logger = logging.getLogger(__name__)


# * SaradCluster:
# ** Definitions:
class SaradCluster(Generic[SI]):
    """Class to define a cluster of SARAD instruments
    that are all connected to one controller

    Properties:
        native_ports
        active_ports
        connected_instruments
        start_time
    Public methods:
        set_native_ports()
        get_native_ports()
        get_active_ports()
        get_connected_instruments()
        update_connected_instruments()
        synchronize(): Stop all instruments, set time, start all measurings
        dump(): Save all properties to a Pickle file"""

    version: str = "3.0"

    @staticmethod
    def get_instrument(device_id, port) -> Optional[SI]:
        """Get the instrument object for an instrument
        with know device_id that is connected to a known port

        Args:
            device_id (str): device id of the instrument encoding
                family, type and serial number
            port (str): id of the serial device the instrument is
                connected to

        Returns:
            SaradInst object
        """
        hid = Hashids()
        family_id = hid.decode(device_id)[0]
        if family_id == 1:
            family_class: Any = DosemanInst
        elif family_id == 2:
            family_class = RscInst
        elif family_id == 5:
            family_class = DacmInst
        else:
            logger.error("Family %s not supported", family_id)
            return None
        family = None
        for family in SaradInst.products:
            if family["family_id"] == family_id:
                break
        try:
            assert family is not None
        except AssertionError:
            logger.error("Family %s not supported", family_id)
            return None
        instrument = family_class()
        instrument.device_id = device_id
        instrument.family = family
        instrument.port = port
        return instrument

    def __init__(
        self,
        native_ports: Optional[List[str]] = None,
        ignore_ports: Optional[List[str]] = None,
    ) -> None:
        if native_ports is None:
            native_ports = []
        self.__native_ports = set(native_ports)
        if ignore_ports is None:
            ignore_ports = []
        self.__ignore_ports = set(ignore_ports)
        self.__start_time = datetime.min
        self.__connected_instruments: List[SI] = []
        self.__active_ports: Set[str] = set()

    # ** Private methods:

    def __iter__(self) -> Iterator[SI]:
        return iter(self.__connected_instruments)

    # ** Public methods:
    # *** synchronize(self, cycles_dict):

    def synchronize(self, cycles_dict: Dict[str, int]) -> bool:
        """Stop measuring cycles of all connected instruments.
        Set instrument time to UTC on all instruments.
        Start measuring cycle on all instruments according to dictionary
        in cycles_dict."""
        for instrument in self.connected_instruments:
            try:
                instrument.stop_cycle()
            except Exception:  # pylint: disable=broad-except
                logger.error("Not all instruments have been stopped as intended.")
                return False
        self.__start_time = datetime.utcnow()
        for instrument in self.connected_instruments:
            try:
                instrument.set_real_time_clock(self.__start_time)
                logger.debug("Clock set to UTC on device %s", instrument.device_id)
                logger.debug("Cycles_dict = %s", cycles_dict)
                if instrument.device_id in cycles_dict:
                    cycle_index = cycles_dict[instrument.device_id]
                    logger.debug(
                        "Cycle_index for device %s is %d",
                        instrument.device_id,
                        cycle_index,
                    )
                else:
                    cycle_index = 0
                instrument.start_cycle(cycle_index)
                logger.debug(
                    "Device %s started with cycle_index %d",
                    instrument.device_id,
                    cycle_index,
                )
            except Exception:  # pylint: disable=broad-except
                logger.error("Failed to set time and start cycles on all instruments.")
                return False
        return True

    # *** update_connected_instruments(self):

    def update_connected_instruments(
        self, ports_to_test=None, ports_to_skip=None
    ) -> List[SI]:
        """Update the list of connected instruments
        in self.__connected_instruments and return this list.

        Args:
            ports_to_test (List[str]): list of serial device ids to test.
                If None, the function will test all serial devices in self.active_ports.
                If given, the function will test serial devices in ports_to_test
                and add newly detected instruments to self.__connected_instruments.
                If no instrument can be found on one of the ports, the instrument
                will be removed from self.__connected_instruments.

        Returns:
            List of instruments added to self.__connected_instruments.
            [] if instruments have been removed.
        """
        logger.debug("[update_connected_instruments]")
        hid = Hashids()
        if ports_to_test is None:
            ports_to_test = self.active_ports
            connected_instruments = []
        else:
            connected_instruments = self.__connected_instruments
        if ports_to_skip is not None:
            connected_instruments = self.__connected_instruments
            logger.debug("Ports to test: %s", ports_to_test)
            logger.debug("Ports to skip: %s", ports_to_skip)
            ports_to_test = list(
                set(ports_to_test).symmetric_difference(set(ports_to_skip))
            )
            logger.debug("Symmetric difference: %s", ports_to_test)
            if ports_to_test == []:
                logger.warning(
                    "Nothing to do. "
                    "Set of serial ports to skip is equal to set of active ports."
                )
                return []
        logger.debug("Connected instruments: %s", connected_instruments)
        added_instruments = []
        logger.debug("%d port(s) to test", len(ports_to_test))
        # We check every active port and try for a connected SARAD instrument.
        # NOTE: The order of tests is very important, because the only
        # difference between RadonScout and DACM GetId commands is the
        # length of reply. Since the reply for DACM is longer than that for
        # RadonScout, the test for RadonScout has always to be made before
        # that for DACM.
        # If ports_to_test is specified, only that list of ports
        # will be checked for instruments,
        # otherwise all available ports will be scanned.
        for family in SaradInst.products:
            if family["family_id"] == 1:
                family_class: Any = DosemanInst
            elif family["family_id"] == 2:
                family_class = RscInst
            elif family["family_id"] == 5:
                family_class = DacmInst
            else:
                continue
            test_instrument = family_class()
            test_instrument.family = family
            ports_with_instruments = []
            if ports_to_test != []:
                logger.debug(ports_to_test)
            for port in ports_to_test:
                logger.debug("Testing port %s for %s.", port, family["family_name"])
                try:
                    test_instrument.port = port
                    if test_instrument.type_id and test_instrument.serial_number:
                        device_id = hid.encode(
                            test_instrument.family["family_id"],
                            test_instrument.type_id,
                            test_instrument.serial_number,
                        )
                        test_instrument.device_id = device_id
                        logger.debug(
                            "%s found on port %s.", family["family_name"], port
                        )
                        added_instruments.append(test_instrument)
                        ports_with_instruments.append(port)
                        if (ports_to_test.index(port) + 1) < len(ports_to_test):
                            test_instrument = family_class()
                            test_instrument.family = family
                except serial.serialutil.SerialException:
                    logger.warning("Tested serial interface not available.")
                except OSError:
                    logger.critical("OSError -- exiting for a restart")
                    os._exit(1)  # pylint: disable=protected-access
            for port in ports_with_instruments:
                ports_to_test.remove(port)
        # remove instruments from self.__connected_instruments
        logger.debug("Remove %s", ports_to_test)
        for instrument in self.__connected_instruments:
            if instrument.port in ports_to_test:
                self.__connected_instruments.remove(instrument)
        # remove duplicates
        self.__connected_instruments = list(
            set(added_instruments).union(set(connected_instruments))
        )
        logger.debug("Connected instruments: %s", self.__connected_instruments)
        return list(set(added_instruments))

    # *** dump(self, file):

    def dump(self, file: IO[bytes]) -> None:
        """Save the cluster information to a file."""
        logger.debug("Pickling mycluster into file.")
        pickle.dump(self, file, pickle.HIGHEST_PROTOCOL)

    # ** Properties:
    # *** active_ports:

    @property
    def active_ports(self) -> List[str]:
        """SARAD instruments can be connected:
        1. by RS232 on a native RS232 interface at the computer
        2. via their built in FT232R USB-serial converter
        3. via an external USB-serial converter (Prolific, Prolific fake, FTDI)
        4. via the SARAD ZigBee coordinator with FT232R"""
        if sys.platform.startswith("win"):
            ports = ["COM%s" % (i + 1) for i in range(256)]
            result = []
            for port in ports:
                try:
                    active_serial = serial.Serial(port)
                    active_serial.close()
                    result.append(port)
                except (OSError, serial.SerialException):
                    pass
            set_of_ports = set(result)
        else:
            active_ports = []
            # Get the list of accessible native ports
            for port in serial.tools.list_ports.comports():
                if port.device in self.__native_ports:
                    active_ports.append(port)
            # FTDI USB-to-serial converters
            active_ports.extend(serial.tools.list_ports.grep("0403"))
            # Prolific and no-name USB-to-serial converters
            active_ports.extend(serial.tools.list_ports.grep("067B"))
            # Actually we don't want the ports but the port devices.
            set_of_ports = set()
            for port in active_ports:
                set_of_ports.add(port.device)
        self.__active_ports = set()
        for port in set_of_ports:
            if port not in self.__ignore_ports:
                self.__active_ports.add(port)
        logger.debug("Native ports: %s", self.__native_ports)
        logger.debug("Ignored ports: %s", self.__ignore_ports)
        logger.debug("Active ports: %s", self.__active_ports)
        return list(self.__active_ports)

    # *** connected_instruments:

    @property
    def connected_instruments(self) -> List[SI]:
        """Return list of connected instruments."""
        return self.__connected_instruments

    # *** native_ports:

    @property
    def native_ports(self) -> Optional[List[str]]:
        """Return the list of all native serial ports (RS-232 ports)
        available at the instrument controller."""
        return list(self.__native_ports)

    @native_ports.setter
    def native_ports(self, native_ports: List[str]) -> None:
        """Set the list of native serial ports that shall be used."""
        self.__native_ports = set(native_ports)

    # *** ignore_ports:

    @property
    def ignore_ports(self) -> Optional[List[str]]:
        """Return the list of all serial ports
        at the instrument controller that shall be ignored."""
        return list(self.__ignore_ports)

    @ignore_ports.setter
    def ignore_ports(self, ignore_ports: List[str]) -> None:
        """Set the list of serial ports that shall be ignored."""
        self.__ignore_ports = set(ignore_ports)

    # *** start_time:

    @property
    def start_time(self) -> datetime:
        """Get a pre-defined start time for all instruments in this cluster."""
        return self.__start_time

    @start_time.setter
    def start_time(self, start_time: datetime) -> None:
        """Set a start time for all instruments in this cluster."""
        self.__start_time = start_time


# * Test environment:
if __name__ == "__main__":
    mycluster: SaradCluster = SaradCluster()
    mycluster.update_connected_instruments()
    logger.debug(mycluster.__dict__)

    for connected_instrument in mycluster:
        print(connected_instrument)

    # Example access on first device
    if len(mycluster.connected_instruments) > 0:
        inst = mycluster.connected_instruments[0]
