import unittest
from local_devices import _walk_bluetooth, _walk_usb


class LocalDeviceTests(unittest.TestCase):
    def test_usb_tree_lists_removable_devices_without_serials(self):
        data = {'SPUSBHostDataType': [{
            '_name': 'USB Bus', 'USBKeyHardwareType': 'Built-in',
            '_items': [
                {'_name': 'USB Hub', 'USBDeviceKeyVendorID': '0x1234',
                 'USBDeviceKeyProductID': '0x1111', 'USBKeyHardwareType': 'Removable'},
                {'_name': 'Traktor Kontrol S2', 'USBDeviceKeyVendorName': 'Native Instruments',
                 'USBDeviceKeyVendorID': '0x17cc', 'USBDeviceKeyProductID': '0x1320',
                 'USBDeviceKeySerialNumber': 'private-serial', 'USBKeyLocationID': '0x1000',
                 'USBKeyHardwareType': 'Removable'}]}]}
        found = []
        _walk_usb(data, found)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['kind'], 'midi')
        self.assertFalse(any('serial' in key.lower() for key in found[0]))

    def test_bluetooth_midi_service_is_classified_and_connection_state_kept(self):
        data = {'devices': [{'device_name': 'Wireless controller',
                             'device_address': '00:11:22:33:44:55',
                             'device_connected': 'attrib_Yes',
                             'device_services': 'Bluetooth MIDI'}]}
        found = []
        _walk_bluetooth(data, found)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['deviceClass'], 'midi')
        self.assertTrue(found[0]['connected'])
        self.assertNotIn('00:11:22:33:44:55', str(found[0]))


if __name__ == '__main__':
    unittest.main()
