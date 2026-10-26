"""Read-only inventory of locally attached USB and Bluetooth peripherals."""
import hashlib
import json
import platform
import re
import subprocess

MIDI_HINT = re.compile(r'\b(midi|kontrol|maschine|launchpad|keystep|keylab|oxygen|mpk|launchkey|novation|arturia|akai|roland|yamaha|korg|dmxking|enttec)\b', re.I)


def _run(args, timeout=8):
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=timeout)
        return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None


def _device_id(interface, identity):
    return 'local-' + hashlib.sha256((interface + ':' + identity).encode()).hexdigest()[:20]


def _record(interface, identity, name, manufacturer='', model='', connected=True, midi=False):
    identity = identity or (manufacturer + ':' + name + ':' + model)
    title = ' '.join(x for x in (manufacturer, name) if x).strip() or 'Unnamed device'
    device_class = 'midi' if midi or MIDI_HINT.search(title) else 'peripheral'
    online = bool(connected)
    return dict(
        id=_device_id(interface, identity), ip='', mac='', manufacturer=manufacturer or 'Unknown manufacturer',
        name=name or title, kind='midi' if device_class == 'midi' else 'peripheral', interface=interface,
        deviceClass=device_class, model=model, firmware='', vendorId='', role='MIDI device' if device_class == 'midi' else f'{interface.upper()} device',
        zone='', notes='', tracked=False, confidence='Operating system device listing', privateMac=False,
        reachability='Online' if online else 'Stale', access='Local device' if online else 'Disconnected', ports=[], webUrl=None,
        lastSeen=None, checkedAt=None, evidence=[f'Listed by the operating system on {interface.upper()}; no device commands were sent.'],
        nextStep='Add to Known to keep this device in the node. A device-specific driver is required before control is available.',
        addedAt=None, connected=online)


def _walk_usb(value, found):
    if isinstance(value, dict):
        name = value.get('_name', '')
        vendor_id = value.get('USBDeviceKeyVendorID', '')
        product_id = value.get('USBDeviceKeyProductID', '')
        hardware = value.get('USBKeyHardwareType', '')
        if name and vendor_id and product_id and hardware.lower() != 'built-in' and 'hub' not in name.lower():
            manufacturer = value.get('USBDeviceKeyVendorName', '')
            location = value.get('USBKeyLocationID', '')
            # Do not retain device serials. Location plus vendor/product keeps identical units distinct.
            identity = f"{location}:{vendor_id}:{product_id}:{name}"
            found.append(_record('usb', identity, name, manufacturer,
                                 value.get('USBDeviceKeyProductVersion', ''), True,
                                 bool(MIDI_HINT.search(name + ' ' + manufacturer))))
        for child in value.values():
            _walk_usb(child, found)
    elif isinstance(value, list):
        for child in value:
            _walk_usb(child, found)


def _yes(value):
    return str(value).lower() in ('true', 'yes', '1', 'attrib_yes', 'connected')


def _walk_bluetooth(value, found):
    if isinstance(value, dict):
        name = value.get('device_name') or value.get('_name')
        address = value.get('device_address') or value.get('device_mac')
        if name and address:
            connected = _yes(value.get('device_connected', value.get('device_isconnected', False)))
            services = str(value.get('device_services', ''))
            found.append(_record('bluetooth', address, name, value.get('device_vendor', ''),
                                 value.get('device_minorType', ''), connected,
                                 bool(MIDI_HINT.search(name + ' ' + services))))
        for child in value.values():
            _walk_bluetooth(child, found)
    elif isinstance(value, list):
        for child in value:
            _walk_bluetooth(child, found)


def discover_local_devices():
    """Return (devices, supported); None devices means the OS listing failed."""
    system = platform.system()
    if system == 'Darwin':
        raw = _run(['/usr/sbin/system_profiler', 'SPUSBHostDataType', 'SPBluetoothDataType', '-json'])
        if raw is None:
            return None, True
        try:
            data = json.loads(raw)
        except ValueError:
            return None, True
        found = []
        _walk_usb(data.get('SPUSBHostDataType', []), found)
        _walk_bluetooth(data.get('SPBluetoothDataType', []), found)
        return _dedupe(found), True
    if system == 'Windows':
        script = ("[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
                  "Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match '^(USB|BTH|BTHLEDEVICE)\\\\' } | "
                  "Select-Object FriendlyName,InstanceId,Class,Status | ConvertTo-Json -Compress")
        raw = _run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script])
        if raw is None:
            return None, True
        try:
            rows = json.loads(raw) if raw.strip() else []
        except ValueError:
            return None, True
        if isinstance(rows, dict):
            rows = [rows]
        found = []
        for row in rows:
            name, instance = row.get('FriendlyName') or '', row.get('InstanceId') or ''
            if not name or re.search(r'root hub|host controller|composite device', name, re.I):
                continue
            interface = 'bluetooth' if instance.upper().startswith(('BTH', 'BTHLEDEVICE')) else 'usb'
            found.append(_record(interface, instance, name, row.get('Class', ''), row.get('Status', ''),
                                 True, bool(MIDI_HINT.search(name + ' ' + row.get('Class', '')))))
        return _dedupe(found), True
    return [], False


def _dedupe(devices):
    by_id = {d['id']: d for d in devices}
    return sorted(by_id.values(), key=lambda d: (d['interface'], d['name'].lower(), d['id']))
