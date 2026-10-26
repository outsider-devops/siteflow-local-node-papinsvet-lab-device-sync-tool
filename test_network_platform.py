import unittest
from unittest.mock import patch
import network_platform as net


class NetworkTests(unittest.TestCase):
    def test_select_and_ambiguous_adapters(self):
        rows = [dict(name='Wi-Fi', ip='192.168.18.5', prefix=24),
                dict(name='Ethernet', ip='10.0.0.2', prefix=24)]
        with patch.object(net, 'adapters', return_value=rows):
            with self.assertRaises(ValueError):
                net.select_network()
            self.assertEqual(net.select_network('10.0.0.2'), ('10.0.0.0/24', '10.0.0.2', 'Ethernet'))

    def test_reject_public_large_and_linklocal_networks(self):
        for ip, prefix in [('8.8.8.8', 24), ('192.168.1.2', 16), ('169.254.1.2', 24)]:
            with patch.object(net, 'adapters', return_value=[dict(name='LAN', ip=ip, prefix=prefix)]):
                with self.assertRaises(ValueError):
                    net.select_network()

    def test_macos_ethernet_and_wifi(self):
        raw = 'en1: flags=8863\n\tinet 192.168.18.5 netmask 0xffffff00\n\tstatus: active\n'
        with patch('platform.system', return_value='Darwin'), patch.object(net, 'command', return_value=raw):
            self.assertEqual(net.select_network(), ('192.168.18.0/24', '192.168.18.5', 'en1'))

    def test_windows_single_adapter_json(self):
        raw = '{"name":"Wi-Fi","ip":"192.168.18.5","prefix":24}'
        with patch('platform.system', return_value='Windows'), patch.object(net, 'command', return_value=raw):
            self.assertEqual(net.select_network()[1], '192.168.18.5')

    def test_windows_arp(self):
        raw = '  192.168.18.80    aa-bb-cc-dd-ee-ff  dynamic\n  192.168.18.255 ff-ff-ff-ff-ff-ff static'
        with patch('platform.system', return_value='Windows'), patch.object(net, 'command', return_value=raw):
            self.assertEqual(net.neighbor_entries('192.168.18.0/24', '192.168.18.5'),
                             {'192.168.18.80': 'aa:bb:cc:dd:ee:ff'})
