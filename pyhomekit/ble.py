"""Contains all of the HAP-BLE classes."""

import logging
import random
from struct import pack, unpack
from typing import (Any, Dict, Iterable, List, Optional, Tuple, Union)  # NOQA pylint: disable=W0611

import bluepy.btle

from . import constants, utils

logger = logging.getLogger(__name__)


class HapBlePduRequestHeader:
    """HAP-BLE PDU Request Header."""

    def __init__(self,
                 cid_sid: bytes,
                 op_code: int,
                 response: bool=True,
                 continuation: bool=False,
                 transaction_id: int=None) -> None:
        """HAP-BLE PDU Request Header.

        Parameters
        ----------
        continuation
            indicates the fragmentation status of the HAP-BLE PDU. False
            indicates a first fragment or no fragmentation.

        response
            indicates whether the PDU is a response (versus a request)

        transation_id
            Transaction Identifier

        op_code
            HAP Opcode field, which indicates the opcode for the HAP Request PDU.

        cid_sid
            Characteristic / Service Instance Identifier is the instance id
            of the characteristic / service for a particular request.
        """
        self.continuation = continuation
        self.response = response
        self.op_code = op_code
        self._transaction_id = transaction_id
        self.cid_sid = cid_sid

    @property
    def control_field(self) -> int:
        """Get formatted Control Field."""
        header = "{continuation}00000{response}0".format(
            continuation=int(self.continuation), response=int(self.response))
        return int(header, 2)

    @property
    def transaction_id(self) -> int:
        """Get the transaction identifier, or generate a new one if none exists.

        The transation ID is an 8 bit number identifying the transaction
        number of this PDU. The TID is randomly generated by the originator
        of the request and is used to match a request/response pair.
        """
        if self._transaction_id is None:
            self._transaction_id = random.SystemRandom().getrandbits(8)
        return self._transaction_id

    @property
    def data(self) -> bytes:
        """Byte representation of the PDU Header.

        Depends on whether it is a continuation header or not."""
        if self.continuation:
            return pack('<BB', self.control_field, self.transaction_id)
        return pack('<BBB', self.control_field, self.op_code,
                    self.transaction_id) + self.cid_sid


class HapBlePduResponseHeader:
    """HAP-BLE PDU Response Header."""

    def __init__(self,
                 status_code: int,
                 transaction_id: int,
                 continuation: bool=False,
                 response: bool=True) -> None:
        """HAP-BLE PDU Response Header.

        Parameters
        ----------
        continuation
            indicates the fragmentation status of the HAP-BLE PDU. False
            indicates a first fragment or no fragmentation.

        response
            indicates whether the PDU is a response (versus a request)

        transaction_id
            Transaction Identifier

        status_code
            HAP Status code for the request.
        """
        self.continuation = continuation
        self.response = response
        self.transaction_id = transaction_id
        self.status_code = status_code

    @classmethod
    def from_data(cls, data: bytes) -> 'HapBlePduResponseHeader':
        """Creates a header from response bytes"""

        # turn control field into its bits
        control_field = bin(unpack('<B', data[:1])[0])[2:].zfill(8)[::-1]
        continuation = control_field[7] == '1'
        response = control_field[1] == '1'
        tid = unpack('<B', data[1:2])[0]
        status_code = unpack('<B', data[2:3])[0]
        return HapBlePduResponseHeader(
            continuation=continuation,
            response=response,
            transaction_id=tid,
            status_code=status_code)

    @property
    def control_field(self) -> int:
        """Get formatted Control Field."""
        header = "{continuation}00000{response}0".format(
            continuation=int(self.continuation), response=int(self.response))
        return int(header, 2)

    @property
    def data(self) -> bytes:
        """Byte representation of the PDU Header."""
        return pack('<BBB', self.control_field, self.transaction_id,
                    self.status_code)


class HapBleError(Exception):
    """HAP Error."""

    def __init__(self,
                 status_code: int=None,
                 name: str=None,
                 message: str=None,
                 *args: str) -> None:
        """HAP Error with appropriate message.

        Parameters
        ----------
        status_code
            the status code of the HAP BLE PDU Response.

        name
            status code name.

        message
            status code message.
        """
        if status_code is None:
            self.name = name
            self.message = message
        else:
            self.status_code = status_code
            self.name = constants.status_code_to_name[status_code]
            self.message = constants.status_code_to_message[status_code]

        super(HapBleError, self).__init__(name, message, *args)

    def __str__(self) -> str:
        """Return formatted error."""
        return "{}: {}".format(self.name, self.message)


class HapCharacteristic:
    """Represents data or an associated behavior of a service.

    The characteristic is defined by a universally unique type, and has additional
    properties that determine how the value of the characteristic can be accessed.
    """

    def __init__(self, characteristic: bluepy.btle.Characteristic) -> None:
        self.characteristic = characteristic
        self.peripheral = characteristic.peripheral
        self._cid = None  # type: Optional[bytes]
        self.hap_format_converter = utils.identity
        self._signature = None  # type: Optional[Dict[str, Any]]

    def setup(self, retry: bool=True, max_attempts: int=5,
              wait_time: int=2) -> Dict[str, Any]:
        """Performs a signature read and reads all characteristic metadata."""
        if retry:
            self._setup_tenacity(
                max_attempts=max_attempts, wait_time=wait_time)

        return self.signature  # read signature pylint: disable=W0104

    @staticmethod
    def _prepare_tlv(param_type: Union[str, int], value: bytes) -> bytes:
        """Formats the TLV into the expected format of the PDU."""
        if isinstance(param_type, str):
            param_type = constants.HAP_param_type_name_to_code[param_type]
        return pack('<BB', param_type, len(value)) + value

    def _request(self,
                 header: HapBlePduRequestHeader,
                 body: List[Tuple[Union[str, int], bytes]]=None) -> None:
        """Perform a HAP read or write request."""
        logger.debug("HAP read/write request.")

        if not body:
            logger.debug("Writing header to characteristic.")
            self.characteristic.write(header.data, withResponse=True)
        else:
            prepared_tlvs = [
                self._prepare_tlv(param_type, value)
                for param_type, value in body
            ]
            body_len = sum(len(b) for b in prepared_tlvs)

            body_concat = b''.join(prepared_tlvs)

            max_len = 512

            # Is a fragmented write necessary?
            if len(header.data) + 2 + body_len <= max_len:
                logger.debug("Writing header + data to characteristic.")
                self.characteristic.write(
                    header.data + pack('<H', body_len) + body_concat,
                    withResponse=True)
            else:
                while body:
                    # Fill fragment
                    fragment_data = b''
                    while body and len(fragment_data) + len(
                            self._prepare_tlv(*body[0])) < max_len:
                        fragment_data += self._prepare_tlv(*body.pop(0))

                    # Split TLV
                    if body:
                        param_type, value = body[0]
                        first_fragment, second_fragment = value[:max_len - len(
                            fragment_data)], value[
                                max_len - len(fragment_data):]
                        body[0] = param_type, second_fragment
                        fragment_data += self._prepare_tlv(
                            param_type, first_fragment)

                    logger.debug(
                        "Writing header + data to characteristic (fragmented)."
                    )
                    # How many TLV to send
                    self.characteristic.write(
                        header.data + pack('<H', len(fragment_data)) +
                        fragment_data,
                        withResponse=True)

                    # Future fragments are continuations
                    header.continuation = True

    def _read(self) -> bytes:
        """Read the value of the characteristic."""
        logger.debug("Reading characteristic value.")
        return self.characteristic.read()

    def write(self,
              request_header: HapBlePduRequestHeader,
              body: List[Tuple[Union[str, int], bytes]]) -> Dict[str, Any]:
        """Perform a HAP Characteristic write.

        Fragmented read/write if required."""
        logger.debug("HAP read/write with OpCode: %s.",
                     constants.HapBleOpCodes()(request_header.op_code))

        self._request(request_header, body)

        response = self._read()

        response_header = self._check_read_response(
            request_header=request_header, response=response)

        if response_header.continuation:
            # TODO: fragmented read
            raise NotImplementedError("Fragmented read not yet supported")

        response_parsed = self._parse_response(response)

        return response_parsed

    def read(self, request_header: HapBlePduRequestHeader) -> Dict[str, Any]:
        """Perform a HAP Characteristic read.

        Fragmented read if required."""

        response_parsed = self.write(request_header, [])

        return response_parsed

    def _setup_tenacity(self, max_attempts: int, wait_time: int) -> None:
        """Adds automatic retrying to functions that need to read from device."""
        reconnect_callback = utils.reconnect_callback_factory(
            peripheral=self.peripheral)

        retry = utils.reconnect_tenacity_retry(reconnect_callback,
                                               max_attempts, wait_time)

        retry_functions = [self._read_cid, self._request, self._read]

        for func in retry_functions:
            name = func.__name__
            setattr(self, name, retry(getattr(self, func.__name__)))

    @property
    def cid(self) -> bytes:
        """Get the Characteristic ID, reading it from the device if required."""
        if self._cid is None:
            self._cid = self._read_cid()
        return self._cid

    @property
    def signature(self) -> Dict[str, Any]:
        """Returns the signature, and adds the attributes."""
        if self._signature is None:
            signature_read_header = HapBlePduRequestHeader(
                cid_sid=self.cid,
                op_code=constants.HapBleOpCodes.Characteristic_Signature_Read,
            )
            self._signature = self.read(signature_read_header)
        return self._signature

    def _read_cid(self) -> bytes:
        """Read the Characteristic ID descriptor."""
        logger.debug("Read characteristic ID descriptor.")
        cid_descriptor = self.characteristic.getDescriptors(
            constants.characteristic_ID_descriptor_UUID)[0]
        return cid_descriptor.read()

    @staticmethod
    def _check_read_response(request_header: HapBlePduRequestHeader,
                             response: bytes) -> HapBlePduResponseHeader:
        """Parses response signature and verifies validity."""

        response_header = HapBlePduResponseHeader.from_data(response)

        if response_header.control_field != request_header.control_field:
            raise ValueError("Invalid control field {}, expected {}.".format(
                response_header.control_field, request_header.control_field),
                             response)
        if response_header.transaction_id != request_header.transaction_id:
            raise ValueError("Invalid transaction ID {}, expected {}.".format(
                response_header.transaction_id, request_header.transaction_id),
                             response)
        if response_header.status_code != constants.HapBleStatusCodes.Success:
            raise HapBleError(status_code=response_header.status_code)

        if len(response) > 3:
            body_length = unpack('<H', response[3:5])[0]
            if len(response[5:]) != body_length:
                raise ValueError("Invalid body length {}, expected {}.".format(
                    len(response[5:]), body_length), response)

        return response_header

    def _parse_response(self, response: bytes) -> Dict[str, Any]:
        """Parse read response and set attributes."""

        logger.debug("Parse read response.")
        attributes = {}
        for body_type, length, bytes_ in utils.iterate_tvl(response[5:]):
            if len(bytes_) != length:
                raise HapBleError(name="Invalid response length")
            name = constants.HAP_param_type_code_to_name[body_type]

            if name in ('GATT_Valid_Range', 'HAP_Step_Value_Descriptor',
                        'Value'):
                converter = self.hap_format_converter
            else:
                converter = constants.HAP_param_name_to_converter[name]

            # Treat GATT_Presentation_Format_Descriptor specially
            if name == 'GATT_Presentation_Format_Descriptor':
                format_code, unit_code = converter(bytes_)
                format_name = constants.format_code_to_name[format_code]
                format_converter = constants.format_name_to_converter[
                    format_name]
                unit_name = constants.unit_code_to_name[unit_code]
                new_attrs = {
                    'HAP_Format': format_name,
                    'HAP_Format_Converter': format_converter,
                    'HAP_Unit': unit_name
                }

            # List of values received in the HAP Format
            elif name == 'GATT_Valid_Range':
                low, high = bytes_[:len(bytes_) // 2], bytes_[
                    len(bytes_) // 2:]
                new_attrs = {
                    'min_value': converter(low),
                    'max_value': converter(high)
                }
            else:
                new_attrs = {name: converter(bytes_)}

            # Add new attributes
            for key, val in new_attrs.items():
                setattr(self, key.lower(), val)
                attributes[key.lower()] = val

        return attributes


class HapAccessory:
    """Accessory"""

    def __init__(self) -> None:
        pass

    def pair(self) -> None:
        pass

    def pair_verify(self) -> None:
        pass

    def save_key(self) -> None:
        pass

    def discover_hap_characteristics(self) -> List[HapCharacteristic]:
        """Discovers all of the HAP Characteristics and performs a signature read on each one."""
        pass

    def get_characteristic(self, name: str, uuid: str) -> HapCharacteristic:
        pass


class HapAccessoryLock(HapAccessory):

    # Required
    def lock_current_state(self) -> int:
        pass

    # Required
    def lock_target_state(self) -> None:
        pass

    # Required for lock management
    def lock_control_point(self) -> Any:
        pass

    def version(self) -> str:
        pass

    # Optional for lock management
    def logs(self) -> str:
        pass

    def audio_feedback(self) -> bytes:
        pass

    def lock_management_auto_security_timeout(self) -> None:
        pass

    def administrator_only_access(self) -> None:
        pass

    def lock_last_known_action(self) -> int:
        pass

    def current_door_state(self) -> int:
        pass

    def motion_detected(self) -> bool:
        pass
