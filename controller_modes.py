"""Verified HTTP mode changes for the lab's ArtDmx2-V3.4 controller."""
import datetime
import http.client
from html.parser import HTMLParser
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlencode

IP = '192.168.18.80'
MAC = '00:00:c8:d8:a2:71'
SOFTWARE = 'SOFTWARE CONTROL'
BYPASS = 'DIRECT BYPASS'
VERIFY_TTL = 75


class DMXForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields, self.select, self.options, self.action = {}, None, [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'form' and a.get('name') == 'dmxconf':
            if a.get('method', '').upper() != 'GET':
                raise ValueError('Unrecognized DMX form method')
            self.action = a.get('action')
        if tag == 'input' and a.get('name'):
            kind = a.get('type', 'text').lower()
            if kind == 'checkbox':
                if 'checked' in a:self.fields[a['name']] = a.get('value', 'on')
            elif kind not in ('submit', 'reset', 'password'):
                # Disabled I fields are deliberately omitted, as in the browser.
                if 'disabled' not in a:self.fields[a['name']] = a.get('value', '')
        if tag == 'select':self.select, self.options = a.get('name'), []
        if tag == 'option' and self.select:
            self.options.append((a.get('value', ''), 'selected' in a))

    def handle_endtag(self, tag):
        if tag == 'select' and self.select:
            if self.options:
                self.fields[self.select] = next((v for v, selected in self.options if selected), self.options[0][0])
            self.select = None


def parse_config(html):
    form = DMXForm();form.feed(html.replace('\x00', ''))
    if form.action != '/cgi/dmx1.htm':raise ValueError('DMX configuration unavailable or login required')
    fields = form.fields
    ports = []
    for n in (1, 2):
        try:
            direction, universe, merge, fps = [int(fields[k + str(n)]) for k in ('M', 'U', 'MG', 'F')]
        except (KeyError, ValueError):raise ValueError('Incomplete DMX configuration')
        if direction not in (0, 1) or not 0 <= universe <= 255 or merge not in range(4) or not 10 <= fps <= 40:
            raise ValueError('Unexpected DMX configuration')
        ports.append(dict(port=n, direction='Output' if direction else 'Input', universe=universe,
                          merge=merge, framerate=fps, unicast='C'+str(n) in fields,
                          destination=fields.get('I'+str(n), fields.get('bc'))))
    mode = 'UNKNOWN'
    if ports[0]['direction'] == 'Output' and ports[1]['direction'] == 'Input' and ports[1]['universe'] == 1:
        mode = SOFTWARE if ports[0]['universe'] == 0 else BYPASS if ports[0]['universe'] == 1 else 'UNKNOWN'
    return dict(mode=mode, ports=ports, fields=fields)


class ControllerModes:
    def __init__(self, monitor, backup_dir, stopped=True, request=None):
        self.monitor, self.backup_dir = monitor, Path(backup_dir)
        self.request = request or self._http
        self.operation = threading.Lock()
        self.user_stopped = stopped
        self.checked = 0
        self.busy = False
        self.state = dict(mode='UNKNOWN', connection='UNKNOWN', error=None, ports=[], autonomy='NOT VERIFIED')

    def _http(self, path):
        conn = http.client.HTTPConnection(IP, 80, timeout=2)
        try:
            conn.request('GET', path, headers={'Cache-Control': 'no-cache'})
            response = conn.getresponse();body = response.read(65537)
            if response.status != 200 or len(body) > 65536:raise ValueError('Unexpected controller HTTP response')
            return body.decode('utf-8', errors='replace')
        finally:conn.close()

    def read(self):
        status = self.request('/')
        if MAC not in status.lower() or 'ArtDmx2-V3.4' not in status or 'ArtNet-1024' not in status:
            raise ValueError('Controller identity or firmware does not match the verified HTTP adapter')
        html = self.request('/cgi/dmx1.htm')
        if 'dmxconf' not in html:
            # This firmware's configured password is empty; no credential is stored.
            self.request('/login?PASS=')
            html = self.request('/cgi/dmx1.htm')
        return parse_config(html)

    def tx_allowed(self):
        return (not self.busy and self.state['connection'] == 'ONLINE' and
                self.state['mode'] == SOFTWARE and time.monotonic()-self.checked < VERIFY_TTL)

    def snapshot(self):
        state = dict(self.state)
        if self.checked and time.monotonic()-self.checked >= VERIFY_TTL:
            state.update(mode='UNKNOWN', connection='UNKNOWN', error='Controller verification expired; TX stopped')
        return dict(state, busy=self.busy, tx='ON' if self.tx_allowed() and not self.monitor.tx_muted else 'STOP',
                    fresh=time.monotonic()-self.checked < VERIFY_TTL)

    def _accept(self, config):
        self.checked = time.monotonic()
        self.state = dict(mode=config['mode'], ports=config['ports'], connection='ONLINE', error=None,
                          checkedAt=datetime.datetime.now(datetime.timezone.utc).isoformat(), autonomy='NOT VERIFIED')
        self.monitor.set_tx_muted(config['mode'] != SOFTWARE or self.user_stopped)

    def _fail(self, exc):
        self.user_stopped = True
        self.monitor.set_tx_muted(True)
        self.checked = 0
        self.state = dict(self.state, mode='UNKNOWN', connection='OFFLINE' if isinstance(exc, OSError) else 'UNKNOWN', error=str(exc))

    def refresh(self):
        if not self.operation.acquire(blocking=False):return self.snapshot()
        try:
            try:self._accept(self.read())
            except (OSError, http.client.HTTPException):
                # One short retry prevents a transient Wi-Fi/repeater timeout
                # from making the controller badge flicker.
                time.sleep(.2)
                self._accept(self.read())
        except (OSError, ValueError, http.client.HTTPException) as exc:self._fail(exc)
        finally:self.operation.release()
        return self.snapshot()

    def switch(self, mode):
        if mode not in (SOFTWARE, BYPASS):raise ValueError('Unknown control mode')
        if not self.operation.acquire(blocking=False):raise ValueError('Controller operation in progress')
        self.busy = True
        # Stop before even reading the configuration. Never send a zero frame.
        self.monitor.set_tx_muted(True)
        try:
            before = self.read()
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = self.backup_dir / ('dmx-'+stamp+'.json')
            backup.write_text(json.dumps(dict(controllerIp=IP, mac=MAC, firmware='ArtDmx2-V3.4', **before), indent=2), encoding='utf-8')
            fields = dict(before['fields'])
            fields.update(M1='1', U1='0' if mode == SOFTWARE else '1', M2='0', U2='1')
            self.request('/cgi/dmx1.htm?' + urlencode(fields))
            after = None
            for attempt in range(3):
                after = self.read()
                if after['mode'] == mode:break
                if attempt < 2:time.sleep(.2)
            if after['mode'] != mode:raise ValueError('Controller readback does not match requested mode; TX remains stopped')
            # Confirm every submitted field, including the settings we must preserve.
            for k, v in fields.items():
                if after['fields'].get(k) != v:raise ValueError('Controller did not preserve field '+k+'; TX remains stopped')
            self.user_stopped = mode != SOFTWARE
            self.busy = False
            self._accept(after)
            self.state['backup'] = backup.name
            return self.snapshot()
        except (OSError, ValueError, http.client.HTTPException) as exc:
            self._fail(exc)
            raise ValueError(str(exc)) from exc
        finally:
            self.busy = False
            self.operation.release()

    def stop(self):
        self.user_stopped = True
        self.monitor.set_tx_muted(True)

    def resume(self):
        with self.operation:
            self.monitor.set_tx_muted(True)
            try:
                config = self.read()
                self._accept(config)
                if config['mode'] != SOFTWARE:raise ValueError('TX requires confirmed SOFTWARE CONTROL')
                self.user_stopped = False
                self.monitor.set_tx_muted(False)
            except (OSError, ValueError, http.client.HTTPException) as exc:
                self._fail(exc);raise ValueError(str(exc)) from exc
