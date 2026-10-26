"""Local IPv4 adapter discovery using built-in Windows/macOS tools."""
import ipaddress
import json
import platform
import re
import subprocess


def command(args):
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ''


def adapters():
    if platform.system() == 'Windows':
        script = ("[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
                  "Get-NetIPConfiguration | Where-Object { $_.NetAdapter.Status -eq 'Up' "
                  "-and $_.NetAdapter.HardwareInterface } | ForEach-Object { "
                  "$a=$_; $_.IPv4Address | ForEach-Object { [pscustomobject]@{ "
                  "name=$a.InterfaceAlias; ip=$_.IPAddress; prefix=$_.PrefixLength } } "
                  "} | ConvertTo-Json -Compress")
        raw = command(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script])
        try:
            rows = json.loads(raw) if raw.strip() else []
        except ValueError:
            rows = []
        return [rows] if isinstance(rows, dict) else rows
    if platform.system() == 'Darwin':
        output = command(['/sbin/ifconfig'])
        rows = []
        for block in re.split(r'(?=^\S+: flags=)', output, flags=re.M):
            name = block.split(':', 1)[0]
            if not name.startswith('en') or 'status: active' not in block:
                continue
            for ip, mask in re.findall(r'inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+)', block):
                net = ipaddress.IPv4Network((ip, str(ipaddress.IPv4Address(int(mask, 16)))), strict=False)
                rows.append(dict(name=name, ip=ip, prefix=net.prefixlen))
        return rows
    raise ValueError('Supported platforms: Windows and macOS.')


def select_network(host_ip=None):
    candidates = []
    for row in adapters():
        ip = ipaddress.IPv4Address(row['ip'])
        net = ipaddress.IPv4Network((str(ip), int(row['prefix'])), strict=False)
        rfc1918 = any(ip in ipaddress.IPv4Network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
        if rfc1918 and 24 <= net.prefixlen <= 30 and (not host_ip or str(ip) == host_ip):
            candidates.append((str(net), str(ip), row['name']))
    if len(candidates) != 1:
        raise ValueError('Connect one private LAN adapter (/24 or smaller), or select it with --host-ip. Available: '
                         + ', '.join(item[1] for item in candidates))
    return candidates[0]


def neighbor_entries(subnet, host_ip):
    if platform.system() == 'Windows':
        output = command(['arp.exe', '-a', '-N', host_ip])
        pairs = re.findall(r'(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F-]{17})\s+', output)
    else:
        output = command(['/usr/sbin/arp', '-an'])
        interface = next((row['name'] for row in adapters() if row['ip'] == host_ip), None)
        pairs = [(ip, mac) for ip, mac, name in re.findall(
            r'\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-fA-F:]+) on (\S+)', output)
                 if name == interface]
    net = ipaddress.IPv4Network(subnet)
    return {ip: ':'.join(p.zfill(2) for p in mac.lower().replace('-', ':').split(':'))
            for ip, mac in pairs if ipaddress.IPv4Address(ip) in net
            and ipaddress.IPv4Address(ip) not in (net.network_address, net.broadcast_address)
            and len(mac.replace('-', ':').split(':')) == 6}
