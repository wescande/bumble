# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# -----------------------------------------------------------------------------
# Imports
# -----------------------------------------------------------------------------
from __future__ import annotations
import enum

from core import CommonErrorCode
from bumble import att
from bumble import device
from bumble import gatt, gatt_client
from typing import Dict, List, Optional, Union


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
class ErrorCode(enum.IntEnum):
    '''See Hearing Access Service 2.4. Attribute Profile error codes.'''

    INVALID_OPCODE = 0x80
    WRITE_NAME_NOT_ALLOWED = 0x81
    PRESET_SYNCHRONIZATION_NOT_SUPPORTED = 0x82
    PRESET_OPERATION_NOT_POSSIBLE = 0x83
    INVALID_PARAMETERS_LENGTH = 0x84


class HearingAidType(enum.IntEnum):
    '''See Hearing Access Service 3.1. Hearing Aid Features.'''

    BINAURAL_HEARING_AID = 0b00
    MONAURAL_HEARING_AID = 0b01
    BANDED_HEARING_AID = 0b10


class PresetSynchronizationSupport(enum.IntEnum):
    '''See Hearing Access Service 3.1. Hearing Aid Features.'''

    PRESET_SYNCHRONIZATION_IS_NOT_SUPPORTED = 0b0
    PRESET_SYNCHRONIZATION_IS_SUPPORTED = 0b1


class IndependentPresets(enum.IntEnum):
    '''See Hearing Access Service 3.1. Hearing Aid Features.'''

    IDENTICAL_PRESET_RECORD = 0b0
    DIFFERENT_PRESET_RECORD = 0b1


class DynamicPresets(enum.IntEnum):
    '''See Hearing Access Service 3.1. Hearing Aid Features.'''

    PRESET_RECORDS_DOES_NOT_CHANGE = 0b0
    PRESET_RECORDS_MAY_CHANGE = 0b1


class WritablePresetsSupport(enum.IntEnum):
    '''See Hearing Access Service 3.1. Hearing Aid Features.'''

    WRITABLE_PRESET_RECORDS_NOT_SUPPORTED = 0b0
    WRITABLE_PRESET_RECORDS_SUPPORTED = 0b1


class HearingAidPresetControlPointOpcode(enum.IntEnum):
    '''See Hearing Access Service 3.3.1 Hearing Aid Preset Control Point operation requirements.'''

    # fmt: off
    READ_PRESETS_REQUEST                     = 0x01
    READ_PRESET_RESPONSE                     = 0x02
    PRESET_CHANGED                           = 0x03
    WRITE_PRESET_NAME                        = 0x04
    SET_ACTIVE_PRESET                        = 0x05
    SET_NEXT_PRESET                          = 0x06
    SET_PREVIOUS_PRESET                      = 0x07
    SET_ACTIVE_PRESET_SYNCHRONIZED_LOCALLY   = 0x08
    SET_NEXT_PRESET_SYNCHRONIZED_LOCALLY     = 0x09
    SET_PREVIOUS_PRESET_SYNCHRONIZED_LOCALLY = 0x0A


class PresetChangedOperation:
    '''See Hearing Access Service 3.2.2.2. Preset Changed operation.'''

    class ChangeId(enum.IntEnum):
        # fmt: off
        GENERIC_UPDATE            = 0x00
        PRESET_RECORD_DELETED     = 0x01
        PRESET_RECORD_AVAILABLE   = 0x02
        PRESET_RECORD_UNAVAILABLE = 0x03

    class Generic:
        prev_index: int
        preset_record: PresetRecord

        def __init__(self, idx, preset):
            self.prev_index = idx
            self.preset_record = preset

    change_id: ChangeId
    additional_parameters: Union[Generic, int]


class PresetChangedOperationDeleted(PresetChangedOperation):
    def __init__(self, index):
        self.change_id = PresetChangedOperation.ChangeId.PRESET_RECORD_DELETED
        self.additional_parameters = index


class PresetChangedOperationAvailable(PresetChangedOperation):
    def __init__(self, index):
        self.change_id = PresetChangedOperation.ChangeId.PRESET_RECORD_AVAILABLE
        self.additional_parameters = index


class PresetChangedOperationUnavailable(PresetChangedOperation):
    def __init__(self, index):
        self.change_id = PresetChangedOperation.ChangeId.PRESET_RECORD_UNAVAILABLE
        self.additional_parameters = index


class PresetRecord:
    '''See Hearing Access Service 2.8. Preset record.'''

    class Property:
        class Writable(enum.IntEnum):
            CANNOT_BE_WRITTEN = 0b0
            CAN_BE_WRITTEN = 0b1

        class IsAvailable(enum.IntEnum):
            IS_UNAVAILABLE = 0b0
            IS_AVAILABLE = 0b1

        writable: Writable
        is_available: IsAvailable

    index: int
    properties: Property
    name: str

    def is_available(self) -> bool:
        return (
            self.properties.is_available
            == PresetRecord.Property.IsAvailable.IS_AVAILABLE
        )


# -----------------------------------------------------------------------------
# Server
# -----------------------------------------------------------------------------
class HearingAccessService(gatt.TemplateService):
    UUID = gatt.GATT_HEARING_ACCESS_SERVICE

    hearing_aid_features: gatt.Characteristic
    hearing_aid_preset_control_point: gatt.Characteristic
    active_preset_index_characteristic: gatt.Characteristic
    active_preset_index: int

    hearing_aid_type: HearingAidType
    preset_synchronization_support: PresetSynchronizationSupport
    independent_presets: IndependentPresets
    dynamic_presets: DynamicPresets
    writable_presets_support: WritablePresetsSupport

    preset_records: Dict[int, PresetRecord]
    read_presets_request_in_progress: bool = False
    # TODO: How can this list be sent when reconnecting to a bonded device
    preset_changed_operations: List[PresetChangedOperation]

    def __init__(self) -> None:
        self.hearing_aid_features = gatt.Characteristic(
            uuid=gatt.GATT_HEARING_AID_FEATURES_CHARACTERISTIC,
            properties=gatt.Characteristic.Properties.READ,  # optional: gatt.Characteristic.Properties.NOTIFY
            permissions=gatt.Characteristic.Permissions.READ_REQUIRES_ENCRYPTION,
            value=gatt.CharacteristicValue(read=self._on_read_hearing_aid_features),
        )
        self.hearing_aid_preset_control_point = gatt.Characteristic(
            uuid=gatt.GATT_HEARING_AID_PRESET_CONTROL_POINT_CHARACTERISTIC,
            properties=(
                gatt.Characteristic.Properties.WRITE
                | gatt.Characteristic.Properties.INDICATE
            ),  # optional: gatt.Characteristic.Properties.NOTIFY when EATT is supported
            permissions=gatt.Characteristic.Permissions.WRITE_REQUIRES_ENCRYPTION,
            value=gatt.CharacteristicValue(
                write=self._on_write_hearing_aid_preset_control_point
            ),
        )
        self.active_preset_index = 0x00
        self.active_preset_index_characteristic = gatt.Characteristic(
            uuid=gatt.GATT_ACTIVE_PRESET_INDEX_CHARACTERISTIC,
            properties=(
                gatt.Characteristic.Properties.READ
                | gatt.Characteristic.Properties.NOTIFY
            ),
            permissions=gatt.Characteristic.Permissions.READ_REQUIRES_ENCRYPTION,
            value=bytes([self.active_preset_index]),
        )

        super().__init__(
            [
                self.hearing_aid_features,
                self.hearing_aid_preset_control_point,
                self.active_preset_index_characteristic,
            ]
        )

    def _on_read_hearing_aid_features(
        self, _connection: Optional[device.Connection]
    ) -> bytes:
        # TODO: Is thit the proper way to concatenate bits ? and is this in correct endianness
        return bytes(
            [
                self.hearing_aid_type << 0
                | self.hearing_aid_type << 2
                | self.preset_synchronization_support << 3
                | self.independent_presets << 4
                | self.dynamic_presets << 5
                | self.writable_presets_support << 6
            ]
        )

    def _on_write_hearing_aid_preset_control_point(
        self, connection: Optional[device.Connection], value: bytes
    ) -> None:
        opcode = HearingAidPresetControlPointOpcode(value[0])
        handler = getattr(self, '_on_' + opcode.name.lower())
        handler(connection, value)

    async def _on_read_presets_request(
        self, connection: Optional[device.Connection], value: bytes
    ):
        assert connection
        start_index = value[1]
        num_preset = value[2]

        sorted_preset_records = [
            self.preset_records[key] for key in sorted(self.preset_records.keys())
        ]
        last_index = sorted_preset_records[-1].index
        if start_index > last_index or start_index == 0x00 or num_preset == 0x00:
            raise att.ATT_Error(CommonErrorCode.OUT_OF_RANGE)

        if self.read_presets_request_in_progress:
            raise att.ATT_Error(CommonErrorCode.PROCEDURE_ALREADY_IN_PROGRESS)

        async def read_preset_response(preset: PresetRecord, is_last: bool):
            # TODO Is this the correct way of sending a ATT_WRITE_RSP ?
            await connection.device.notify_subscriber(
                connection,
                self.hearing_aid_preset_control_point,
                value=bytes(
                    [
                        HearingAidPresetControlPointOpcode.READ_PRESET_RESPONSE,
                        is_last,
                        preset,
                    ]
                ),  # TODO preset doesn't work here
            )

        self.read_presets_request_in_progress = True
        for record in sorted_preset_records:
            if record.index < start_index:
                continue

            num_preset -= 1
            is_last = num_preset == 0 or record.index == last_index
            await read_preset_response(record, is_last)
            if is_last:
                break

        self.read_presets_request_in_progress = False

    def preset_changed(self, index):
        assert self.dynamic_presets == DynamicPresets.PRESET_RECORDS_MAY_CHANGE
        # TODO implement
        return

    def generic_update(self, index):
        # TODO implement
        return

    def delete_preset(self, index):
        self.preset_changed_operations.append(PresetChangedOperationDeleted(index))
        # TODO notify all devices ! How so ?

    def available_preset(self, index):
        self.preset_changed_operations.append(PresetChangedOperationAvailable(index))
        # TODO notify all devices ! How so ?

    def unavailable_preset(self, index):
        self.preset_changed_operations.append(PresetChangedOperationUnavailable(index))
        # TODO notify all devices ! How so ?

    # def _on_read_preset_response(self):
    #     # Server should not receive a preset response but initiate them
    #     assert False

    # def _on_preset_changed(self, value: bytes):
    #     # Server should not receive a preset response but initiate them
    #     assert False

    def _on_write_preset_name(
        self, connection: Optional[device.Connection], value: bytes
    ):
        assert (
            self.writable_presets_support
            == WritablePresetsSupport.WRITABLE_PRESET_RECORDS_SUPPORTED
        )
        if self.read_presets_request_in_progress:
            raise att.ATT_Error(CommonErrorCode.PROCEDURE_ALREADY_IN_PROGRESS)

        index = value[1]
        preset = self.preset_records.get(index, None)
        if (
            not preset
            or preset.properties.writable
            == PresetRecord.Property.Writable.CANNOT_BE_WRITTEN
        ):
            raise att.ATT_Error(ErrorCode.WRITE_NAME_NOT_ALLOWED)

        # TODO Is this a correct way of decoding utf8
        name = value[2:].decode('utf-8')
        if not name or len(name) > 40:
            raise att.ATT_Error(ErrorCode.INVALID_PARAMETERS_LENGTH)

        preset.name = name

        self.preset_changed(index)

    async def notify_active_preset(self, connection: device.Connection):
        # TODO Is this the correct way to notify ?
        await connection.device.notify_subscriber(
            connection,
            self.active_preset_index_characteristic,
            value=bytes([self.active_preset_index]),
        )
        # TODO broadcast to other connections ?

    async def set_active_preset(
        self, connection: Optional[device.Connection], value: bytes
    ) -> bool:
        assert connection
        index = value[1]
        preset = self.preset_records.get(index, None)
        if (
            not preset
            or preset.properties.is_available
            == PresetRecord.Property.IsAvailable.IS_AVAILABLE
        ):
            raise att.ATT_Error(ErrorCode.PRESET_OPERATION_NOT_POSSIBLE)

        self.active_preset_index = index
        await self.notify_active_preset(connection)
        return True

    async def _on_set_active_preset(
        self, connection: Optional[device.Connection], value: bytes
    ):
        await self.set_active_preset(connection, value)

    async def set_next_preset(
        self, connection: Optional[device.Connection], is_reverse
    ) -> bool:
        assert connection

        if self.active_preset_index == 0x00:
            raise att.ATT_Error(ErrorCode.PRESET_OPERATION_NOT_POSSIBLE)

        first_preset: Optional[PresetRecord] = None  # To loop to first preset
        next_preset: Optional[PresetRecord] = None
        for index, record in sorted(self.preset_records.items(), reverse=is_reverse):
            if not record.is_available():
                continue
            if first_preset == None:
                first_preset = record
            if index <= self.active_preset_index:
                continue
            next_preset = record
            break

        if not first_preset:  # if there is no first, there will be no next either
            raise att.ATT_Error(ErrorCode.PRESET_OPERATION_NOT_POSSIBLE)

        if next_preset:
            self.active_preset_index = next_preset.index
        else:
            self.active_preset_index = first_preset.index
        await self.notify_active_preset(connection)
        return True

    async def _on_set_next_preset(self, connection: Optional[device.Connection]):
        await self.set_next_preset(connection, False)

    async def _on_set_previous_preset(self, connection: Optional[device.Connection]):
        await self.set_next_preset(connection, True)

    async def _on_set_active_preset_synchronized_locally(
        self, connection: Optional[device.Connection], value: bytes
    ):
        assert (
            self.preset_synchronization_support
            == PresetSynchronizationSupport.PRESET_SYNCHRONIZATION_IS_SUPPORTED
        )
        await self.set_active_preset(connection, value)
        # TODO inform other server of the change

    async def _on_set_next_preset_synchronized_locally(
        self, connection: Optional[device.Connection]
    ):
        assert (
            self.preset_synchronization_support
            == PresetSynchronizationSupport.PRESET_SYNCHRONIZATION_IS_SUPPORTED
        )
        await self.set_next_preset(connection, False)
        # TODO inform other server of the change

    async def _on_set_previous_preset_synchronized_locally(
        self, connection: Optional[device.Connection]
    ):
        assert (
            self.preset_synchronization_support
            == PresetSynchronizationSupport.PRESET_SYNCHRONIZATION_IS_SUPPORTED
        )
        await self.set_next_preset(connection, True)
        # TODO inform other server of the change


# -----------------------------------------------------------------------------
# Client
# -----------------------------------------------------------------------------
class HearingAccessServiceProxy(gatt_client.ProfileServiceProxy):
    SERVICE_CLASS = HearingAccessService

    hearing_aid_preset_control_point: gatt_client.CharacteristicProxy

    def __init__(self, service_proxy: gatt_client.ServiceProxy) -> None:
        self.service_proxy = service_proxy

        self.hearing_aid_preset_control_point = (
            service_proxy.get_characteristics_by_uuid(
                gatt.GATT_HEARING_AID_PRESET_CONTROL_POINT_CHARACTERISTIC
            )[0]
        )
