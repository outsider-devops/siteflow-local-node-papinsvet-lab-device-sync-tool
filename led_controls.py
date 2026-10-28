"""Imported SP801E pixel fixtures and editable virtual-DMX adapters."""
import copy,math,time
from midi_inputs import mapped_values as midi_mapped_values, LastChange
FUNCTIONS=['Master','Red','Green','Blue','White','Amber','UV','Strobe']
SP801E_PORT_COUNT=4
SP801E_MAX_PIXELS_PER_PORT=1024  # Hardware profile cap; ArtPoll allocation can impose a lower per-port limit.

def default_canvases():
    """Seed the known installation geometry without changing its Art-Net output patch."""
    return [dict(id='canvas:matrix-main',name='Main Matrix',controllerId='20:e7:c8:f6:68:80',controllerIp='192.168.18.157',port=1,
                 startPixel=1,width=5,height=40,pixelCount=200,order='serpentine',startCorner='bottom-left',
                 scanAxis='vertical',colorOrder='BRG',enabled=True,firstUniverse=16,universeCount=2,
                 mappingVerified=False)]

def validate_canvases(items, existing=None):
    if not isinstance(items,list) or len(items)>64:raise ValueError('Canvas list must contain at most 64 canvases')
    clean=[];used_ids=set();ranges={};universe_ranges={}
    for raw in items:
        if not isinstance(raw,dict):raise ValueError('Invalid canvas entry')
        cid=raw.get('id');name=raw.get('name');controller_id=raw.get('controllerId');ip=raw.get('controllerIp');port=raw.get('port')
        start=raw.get('startPixel');width=raw.get('width');height=raw.get('height')
        shape=raw.get('shape','rectangle')
        order=raw.get('order','serpentine');corner=raw.get('startCorner','bottom-left');axis=raw.get('scanAxis','vertical')
        color_order=raw.get('colorOrder','BRG')
        if not isinstance(cid,str) or not cid or len(cid)>80 or cid in used_ids:raise ValueError('Canvas IDs must be unique')
        if not isinstance(name,str) or not name.strip() or len(name)>80:raise ValueError('Canvas needs a name (max 80 characters)')
        if not isinstance(ip,str) or not ip:raise ValueError('Canvas must belong to a controller')
        if not isinstance(controller_id,str) or not controller_id or len(controller_id)>80:raise ValueError('Canvas must be bound to a controller MAC identity')
        if type(port) is not int or not 1<=port<=SP801E_PORT_COUNT:raise ValueError('Choose output port P1–P4')
        if type(start) is not int or type(width) is not int or type(height) is not int or start<1 or width<1 or height<1:
            raise ValueError('Pixel start and canvas dimensions must be positive whole numbers')
        if shape not in ('rectangle','single','circle','line','irregular'):raise ValueError('Choose rectangle, single pixel, line, circle, or irregular canvas')
        lines=raw.get('lines',[]) if shape=='irregular' else []
        if shape=='irregular':
            if not isinstance(lines,list) or not 2<=len(lines)<=1024:raise ValueError('Irregular canvas needs 2–1024 mapped lines')
            if axis not in ('vertical','horizontal'):raise ValueError('Choose vertical or horizontal mapped lines')
            edges={'top','left','right','bottom'}
            clean_lines=[];intervals=[];line_counts=[]
            for line in lines:
                if not isinstance(line,dict):raise ValueError('Each mapped line needs two address points and edge labels')
                a=line.get('startPixel');b=line.get('endPixel');a_edge=line.get('startEdge');b_edge=line.get('endEdge')
                if type(a) is not int or type(b) is not int or not 1<=a<=1024 or not 1<=b<=1024 or a==b:
                    raise ValueError('Mapped-line points must be different pixel addresses within 1–1024')
                if a_edge not in edges or b_edge not in edges or a_edge==b_edge:
                    raise ValueError('Each mapped line must connect two different canvas edges')
                lo,hi=sorted((a,b));intervals.append((lo,hi));line_counts.append(hi-lo+1)
                clean_lines.append(dict(startPixel=a,endPixel=b,startEdge=a_edge,endEdge=b_edge))
            intervals.sort();cursor=intervals[0][0]
            for lo,hi in intervals:
                if lo!=cursor:raise ValueError('Mapped lines must cover one continuous address range without overlaps or gaps')
                cursor=hi+1
            count=sum(line_counts);start=intervals[0][0];end=intervals[-1][1]
            if count!=end-start+1:raise ValueError('Mapped lines do not form one continuous pixel address range')
            if count>SP801E_MAX_PIXELS_PER_PORT:raise ValueError('Canvas exceeds the SP801E limit of 1024 pixels per port')
            if axis=='vertical':width=len(lines);height=max(line_counts)
            else:width=max(line_counts);height=len(lines)
            if width*height>SP801E_MAX_PIXELS_PER_PORT*1024:raise ValueError('Irregular canvas bounding rectangle is too large')
            order='mapped'
        else:
            count=width*height;end=start+count-1
        if shape=='single' and (width,height,count)!=(1,1,1):raise ValueError('A single-pixel canvas must be exactly 1×1')
        if shape=='circle' and (height!=1 or width<3):raise ValueError('A circle canvas needs two distinct addresses and at least 3 pixels')
        if shape=='line' and height!=1:raise ValueError('A line canvas must use one row of consecutive pixels')
        if count>SP801E_MAX_PIXELS_PER_PORT or end>SP801E_MAX_PIXELS_PER_PORT:raise ValueError('Canvas exceeds the SP801E limit of 1024 pixels per port')
        if shape!='irregular' and order not in ('serpentine','linear'):raise ValueError('Choose serpentine or linear addressing')
        if corner not in ('bottom-left','bottom-right','top-left','top-right'):raise ValueError('Choose one of the four start corners')
        if axis not in ('vertical','horizontal'):raise ValueError('Choose a valid scan direction')
        if color_order not in ('RGB','RBG','GRB','GBR','BRG','BGR'):raise ValueError('Choose a valid pixel color-channel order')
        enabled=raw.get('enabled',True)
        if type(enabled) is not bool:raise ValueError('Invalid canvas enabled state')
        occupied=ranges.setdefault((controller_id,port),[])
        if any(start<=b and end>=a for a,b in occupied):raise ValueError(f'Pixel range P{port} {start}–{end} overlaps another canvas')
        occupied.append((start,end))
        first=raw.get('firstUniverse');universes=raw.get('universeCount')
        if first is not None and (type(first) is not int or not 0<=first<=32767):raise ValueError('Art-Net start Universe must be 0–32767')
        if universes is not None and (type(universes) is not int or universes<1 or universes>8):raise ValueError('Universe count must be 1–8')
        if first is None or universes is None:raise ValueError('Assign an Art-Net Universe range to every canvas')
        needed_universes=math.ceil((start+count-1)*3/512)
        if universes != needed_universes:raise ValueError(f'This canvas address range uses {needed_universes} Art-Net Universes at 3 slots per pixel')
        if first is not None and first+universes-1>32767:raise ValueError('Art-Net Universe range exceeds 32767')
        occupied_universes=universe_ranges.setdefault(controller_id,[]);last=first+universes-1
        same_port=[row for row in occupied_universes if row[0]==port]
        if same_port and any(row[1]!=first for row in same_port):raise ValueError(f'All canvases on port P{port} must share the same Art-Net start Universe')
        if any(row[0]!=port and first<=row[2] and last>=row[1] for row in occupied_universes):raise ValueError(f'Art-Net Universe range {first}–{last} overlaps another port on this controller')
        occupied_universes.append((port,first,last))
        used_ids.add(cid)
        clean.append(dict(id=cid,name=name.strip(),controllerId=controller_id,controllerIp=ip,port=port,startPixel=start,width=width,height=height,shape=shape,
                          pixelCount=count,order=order,startCorner=corner,scanAxis=axis,enabled=enabled,
                          lines=clean_lines if shape=='irregular' else None,colorOrder=color_order,firstUniverse=first,universeCount=universes,mappingVerified=bool(raw.get('mappingVerified',False))))
    return clean

def defaults():
    base=dict(count=144,order='GRB',limit=32,packing=512,mapping=FUNCTIONS,transport='artnet',controllerIp='192.168.18.3')
    items=[dict(copy.deepcopy(base),id='led:strip'+str(i),name='LED Strip P'+str(i),port=i,baseUniverse=1,firstUniverse=2*i-1,mirror=False) for i in range(1,5)]
    items.append(dict(id='led:matrix',name='Vertical LED Matrix',controllerId='20:e7:c8:f6:68:80',controllerIp='192.168.18.157',port=1,count=200,order='BRG',limit=64,packing=512,mapping=list(FUNCTIONS),transport='artnet',baseUniverse=16,firstUniverse=16,mirror=True,renderMode='pixel-fill',reverse=False,canvasTargets=[dict(id='canvas:matrix-main')]))
    return items

def virtual_fixture(f,universe=1,canvas=None):
    return dict(id=canvas['id'] if canvas else f['id'],name=f"{canvas['name']} · P{canvas['port']}" if canvas else f['name'],
                universe=universe,start=1,channels=[dict(function=k,default=0) for k in f['mapping']],
                mappingPreset='custom',virtualLED=True,virtualCanvas=bool(canvas),controllerIp=f['controllerIp'])

def validate_mapping(data,old,available_canvases=None):
    mapping=data.get('mapping');limit=data.get('limit',old['limit'])
    if not isinstance(mapping,list) or not 1<=len(mapping)<=16 or any(k not in FUNCTIONS+['Unknown'] for k in mapping):raise ValueError('Use 1–16 virtual channels with known functions')
    known=[k for k in mapping if k!='Unknown']
    if len(known)!=len(set(known)):raise ValueError('Each function may appear once')
    if type(limit) is not int or not 0<=limit<=255:raise ValueError('Brightness limit must be 0–255')
    mode=data.get('renderMode',old.get('renderMode','parled'));reverse=data.get('reverse',old.get('reverse',False))
    if mode not in ('parled','pixel-fill','screen') or mode in ('pixel-fill','screen') and old['id']!='led:matrix':raise ValueError('Pixel rendering presets are available for LED Matrix only')
    if type(reverse) is not bool:raise ValueError('Invalid pixel direction')
    targets=data.get('canvasTargets',old.get('canvasTargets',[]))
    if old['id']=='led:matrix':
        if not isinstance(targets,list):raise ValueError('Choose the existing canvases used by this emulator')
        canvases={c.get('id'):c for c in (available_canvases or []) if c.get('controllerId')==old.get('controllerId')}
        clean=[];seen=set()
        for target in targets:
            if not isinstance(target,dict) or target.get('id') not in canvases:raise ValueError('A selected canvas no longer exists on this controller')
            cid=target['id']
            if cid in seen:raise ValueError('Select each canvas only once')
            seen.add(cid);clean.append(dict(id=cid))
        targets=clean
    return dict(old,mapping=list(mapping),limit=limit,renderMode=mode,reverse=reverse,canvasTargets=targets if old['id']=='led:matrix' else old.get('canvasTargets'))

def strobe_open(value,now):
    """LED emulation: 0 and 255 are steady; intermediate values pulse."""
    return not (0<value<255) or (now*(.5+9.5*value/254))%1<.5

def rgb_value(f,values,now):
    roles=dict(zip(f['mapping'],values));master=roles.get('Master',255)/255;white=roles.get('White',0);amber=roles.get('Amber',0)
    rgb=[min(255,roles.get('Red',0)+white+amber),min(255,roles.get('Green',0)+white+round(amber*96/255)),min(255,roles.get('Blue',0)+white)]
    strobe=roles.get('Strobe',0)
    if not strobe_open(strobe,now):master=0
    return dict(zip('RGB',[round(v*master*f['limit']/255) for v in rgb]))

def pixel_payload(f,values,now):
    mode=f.get('renderMode')
    if mode not in ('pixel-fill','screen'):
        rgb=rgb_value(f,values,now)
        return [rgb[k] for k in f['order']]*f['count']
    roles=dict(zip(f['mapping'],values));count=f['count'];master=roles.get('Master',255)/255*f['limit']/255
    strobe=roles.get('Strobe',0)
    if not strobe_open(strobe,now):master=0
    layout=f.get('matrixLayout') or dict(pixelsPerRow=count,rows=1,serpentine=False,axis='horizontal')
    if mode=='screen':
        length=layout['pixelsPerRow'];rows=layout['rows'];axis=layout.get('axis','vertical');serpentine=layout.get('serpentine',False)
        payload=[]
        for physical in range(count):
            band,offset=divmod(physical,length)
            corner=layout.get('startCorner','bottom-left')
            from_right=corner.endswith('right');from_top=corner.startswith('top')
            reverse=(from_right if axis=='horizontal' else from_top) ^ (serpentine and band%2==1)
            position=length-1-offset if reverse else offset
            if axis=='vertical':
                row_from_bottom=position
            else:
                row_from_bottom=rows-1-band if from_top else band
            coverage={role:max(0,min(1,roles.get(role,0)*({ 'vertical':length, 'horizontal':rows }[axis])/255-row_from_bottom)) for role in ('Red','Green','Blue','White','Amber','UV')}
            rgb=[coverage['Red']+coverage['White']+coverage['Amber']+.55*coverage['UV'],coverage['Green']+coverage['White']+.376*coverage['Amber'],coverage['Blue']+coverage['White']+coverage['UV']]
            color=dict(zip('RGB',[round(min(1,v)*255*master) for v in rgb]))
            payload.extend(color[k] for k in f['order'])
        return payload
    payload=[]
    for physical in range(count):
        i=count-1-physical if f.get('reverse') else physical
        coverage={role:max(0,min(1,roles.get(role,0)*count/255-i)) for role in ('Red','Green','Blue','White','Amber','UV')}
        rgb=[coverage['Red']+coverage['White']+coverage['Amber']+.55*coverage['UV'],
             coverage['Green']+coverage['White']+.376*coverage['Amber'],
             coverage['Blue']+coverage['White']+coverage['UV']]
        color=dict(zip('RGB',[round(min(1,v)*255*master) for v in rgb]))
        payload.extend(color[k] for k in f['order'])
    return payload

def irregular_canvas_payload(f,values,now,canvas):
    """Render mapped, variable-length lines into the saved physical address order."""
    roles=dict(zip(f['mapping'],values));width=canvas['width'];height=canvas['height'];axis=canvas['scanAxis']
    master=roles.get('Master',255)/255*f['limit']/255;strobe=roles.get('Strobe',0)
    if not strobe_open(strobe,now):master=0
    payload=[0]*(canvas['pixelCount']*3);line_index=0
    for line in canvas['lines']:
        a=line['startPixel'];b=line['endPixel'];count=abs(b-a)+1
        for j,address in enumerate(range(a,b+1) if b>=a else range(a,b-1,-1)):
            fraction=0 if count<=1 else j/(count-1)
            lane_x=round(line_index*max(0,width-1)/max(1,len(canvas['lines'])-1))
            lane_y=round(line_index*max(0,height-1)/max(1,len(canvas['lines'])-1))
            def edge_point(edge):
                if edge=='top':return lane_x,0
                if edge=='bottom':return lane_x,height-1
                if edge=='left':return 0,lane_y
                return width-1,lane_y
            ax,ay=edge_point(line['startEdge']);bx,by=edge_point(line['endEdge'])
            x=ax+(bx-ax)*fraction;y=ay+(by-ay)*fraction
            # Each RGB function fills from the lower edge through the calibrated geometry.
            span=height if axis=='vertical' else max(1,width)
            row_from_bottom=max(0,(height-1-y) if axis=='vertical' else x)
            coverage={role:max(0,min(1,roles.get(role,0)*span/255-row_from_bottom)) for role in ('Red','Green','Blue','White','Amber','UV')}
            rgb=[coverage['Red']+coverage['White']+coverage['Amber']+.55*coverage['UV'],coverage['Green']+coverage['White']+.376*coverage['Amber'],coverage['Blue']+coverage['White']+coverage['UV']]
            color=dict(zip('RGB',[round(min(1,v)*255*master) for v in rgb]))
            offset=(address-canvas['startPixel'])*3
            payload[offset:offset+3]=[color[k] for k in f['order']]
        line_index+=1
    return payload

def canvas_pixel_payload(f,values,now,canvas,orientation):
    """Fill selected canvas geometry along its chosen display orientation.

    Pixel address order remains owned by the canvas mapping (including its
    serpentine wiring). This orientation only controls the rendered fill axis.
    """
    if canvas.get('shape')=='irregular':
        return irregular_canvas_payload(f,values,now,dict(canvas,scanAxis=orientation))
    roles=dict(zip(f['mapping'],values));width=canvas['width'];height=canvas['height']
    master=roles.get('Master',255)/255*f['limit']/255;strobe=roles.get('Strobe',0)
    if not strobe_open(strobe,now):master=0
    payload=[];snake=canvas.get('order')=='serpentine';corner=canvas.get('startCorner','bottom-left')
    for index in range(canvas['pixelCount']):
        if canvas.get('scanAxis')=='horizontal':
            band,offset=divmod(index,width);reverse=corner.endswith('right') ^ (snake and band%2==1)
            x=width-1-offset if reverse else offset
            y=band if corner.startswith('top') else height-1-band
        else:
            band,offset=divmod(index,height);reverse=corner.startswith('top') ^ (snake and band%2==1)
            y=offset if reverse else height-1-offset
            x=width-1-band if corner.endswith('right') else band
        if orientation=='vertical':
            extent=height;progress=height-1-y
        else:
            extent=width;progress=x
        coverage={role:max(0,min(1,roles.get(role,0)*extent/255-progress)) for role in ('Red','Green','Blue','White','Amber','UV')}
        rgb=[coverage['Red']+coverage['White']+coverage['Amber']+.55*coverage['UV'],
             coverage['Green']+coverage['White']+.376*coverage['Amber'],
             coverage['Blue']+coverage['White']+coverage['UV']]
        color=dict(zip('RGB',[round(min(1,v)*255*master) for v in rgb]))
        payload.extend(color[k] for k in f['order'])
    return payload


def controller_frames(fixtures,values,now,render_modes=None,canvases=None):
    frames={}
    for f in fixtures:
        if f['id'] not in values and not (f['id']=='led:matrix' and any(c.get('id') in values for c in (canvases or []))):continue
        f=dict(f,renderMode=(render_modes or {}).get(f['id'],f.get('renderMode','parled')))
        ip=f['controllerIp'];base=f['baseUniverse']
        controller_id=f.get('controllerId')
        selected={item.get('id'):item for item in f.get('canvasTargets',[]) if isinstance(item,dict)}
        port_canvases=[c for c in (canvases or []) if c.get('controllerId')==controller_id and (c.get('id') in values or c.get('id') in selected and f['id'] in values)] if f['id']=='led:matrix' and controller_id else []
        if f['id']=='led:matrix' and not controller_id:
            port_canvases=[c for c in (canvases or []) if not c.get('controllerId') and c.get('controllerIp')==ip and (c.get('id') in values or c.get('id') in selected and f['id'] in values)]
        # Matrix output is explicitly patched by Canvas only. The former
        # legacy mirror fallback emitted fixture values on default universes
        # even when the user had deleted every Canvas.
        if f['id']=='led:matrix' and not port_canvases:continue
        if port_canvases:
            grouped={}
            for c in port_canvases:
                canvas_values=values.get(c['id'],values.get(f['id']))
                canvas_mode=(render_modes or {}).get(c['id'],f['renderMode'])
                port=c['port']
                end_pixel=c['startPixel']+c['pixelCount']-1
                target=grouped.setdefault(port,dict(firstUniverse=c['firstUniverse'],pixels=[0]*(end_pixel*3)))
                if target['firstUniverse']!=c['firstUniverse']:
                    raise ValueError(f'Canvas Universe mismatch on output P{port}')
                if len(target['pixels'])<end_pixel*3:target['pixels'].extend([0]*(end_pixel*3-len(target['pixels'])))
                if not c.get('enabled',True):continue
                orientation=c.get('scanAxis','vertical')
                layout=dict(pixelsPerRow=c['height'] if orientation=='vertical' else c['width'],
                            rows=c['width'] if orientation=='vertical' else c['height'],
                            serpentine=False,axis=orientation,startCorner=c['startCorner'])
                canvas_fixture=dict(f,count=c['pixelCount'],matrixLayout=layout,reverse=False,order=c.get('colorOrder',f.get('order','BRG')))
                if canvas_mode=='parled':
                    rgb=rgb_value(canvas_fixture,canvas_values,now)
                    canvas_payload=[rgb[channel] for channel in canvas_fixture['order']]*c['pixelCount']
                else:
                    canvas_payload=canvas_pixel_payload(canvas_fixture,canvas_values,now,c,orientation)
                offset=(c['startPixel']-1)*3
                grouped[port]['pixels'][offset:offset+len(canvas_payload)]=canvas_payload
            for port,patch in grouped.items():
                pixels=patch['pixels'];start=patch['firstUniverse']
                for offset,value in enumerate(pixels):
                    universe=start+offset//512
                    frame=frames.setdefault((ip,universe),[0]*512)
                    frame[offset%512]=max(frame[offset%512],value)
    return frames


def merge_pixel_frames_htp(base,layer):
    """Combine rendered pixel layers before ArtSync using per-channel HTP."""
    combined={key:list(values) for key,values in base.items()}
    for key,values in layer.items():
        target=combined.setdefault(key,[0]*512)
        if len(target)!=len(values):raise ValueError('Pixel frame length mismatch')
        for index,value in enumerate(values):
            if value>target[index]:target[index]=value
    return combined


def complete_controller_frames(frames,controller_ip,output_ranges):
    """Send a complete SP801E Universe set whenever that controller has output.

    The working locator and full-port test send every configured Universe before
    ArtSync. A partial Canvas frame leaves the remaining Universes out of that
    cycle, which some controller configurations do not latch to the LEDs.
    """
    if not controller_ip or not any(ip==controller_ip for ip,_ in frames):
        return frames
    if not isinstance(output_ranges,list) or len(output_ranges)!=4:
        raise ValueError('Controller output Universe map is unavailable')
    completed=dict(frames)
    for universes in output_ranges:
        if not isinstance(universes,list) or not universes:
            raise ValueError('Controller output Universe map is incomplete')
        for universe in universes:
            completed.setdefault((controller_ip,universe),[0]*512)
    return completed


def locator_frames(locator,previous=None):
    """Build transient one-pixel calibration frames and clear the last point."""
    frames={}
    def blank(target):
        if not target:return
        for universe in target.get('reportedUniverses') or (target['firstUniverse'],):
            frames.setdefault((target['ip'],universe),[0]*512)
    blank(previous)
    if locator:
        target=locator;blank(target)
        offset=(target['pixel']-1)*3
        for channel in range(3):
            absolute=offset+channel
            frames[(target['ip'],target['firstUniverse']+absolute//512)][absolute%512]=255
    return frames

def output_address_test_frames(ip,output_ranges,color_orders,color):
    """Exercise every Universe patched to each port, including unused tail slots."""
    frames={}
    for port,universes in enumerate(output_ranges):
        if not universes:continue
        red_offset=color_orders[port].index('R')
        for index,universe in enumerate(universes):
            base=index*512
            frames[(ip,universe)]=[255 if base+slot<SP801E_MAX_PIXELS_PER_PORT*3 and (color=='white' or color=='red' and (base+slot)%3==red_offset) else 0 for slot in range(512)]
    return frames

def canvas_sweep_addresses(canvas):
    """Return full-width horizontal and full-height vertical pixel strips."""
    pixels=[];width=canvas['width'];height=canvas['height'];axis=canvas.get('scanAxis','vertical')
    if canvas.get('shape')=='irregular' and isinstance(canvas.get('lines'),list):
        lines=canvas['lines']
        for lane,line in enumerate(lines):
            a=line['startPixel'];b=line['endPixel'];count=abs(b-a)+1
            lane_x=round(lane*max(0,width-1)/max(1,len(lines)-1));lane_y=round(lane*max(0,height-1)/max(1,len(lines)-1))
            def edge_point(edge):
                if edge=='top':return lane_x,0
                if edge=='bottom':return lane_x,height-1
                if edge=='left':return 0,lane_y
                return width-1,lane_y
            ax,ay=edge_point(line['startEdge']);bx,by=edge_point(line['endEdge'])
            for offset,address in enumerate(range(a,b+1) if b>=a else range(a,b-1,-1)):
                fraction=0 if count<=1 else offset/(count-1)
                pixels.append((address,ax+(bx-ax)*fraction,ay+(by-ay)*fraction))
    elif canvas.get('shape')=='circle':
        count=canvas['pixelCount']
        for index in range(count):
            angle=-math.pi/2+index/count*math.pi*2
            pixels.append((canvas['startPixel']+index,(width-1)/2+math.cos(angle)*(width-1)/2,(height-1)/2+math.sin(angle)*(height-1)/2))
    else:
        for index in range(canvas['pixelCount']):
            if axis=='horizontal':
                band=math.floor(index/width);offset=index%width;from_right=canvas.get('startCorner','bottom-left').endswith('right');from_top=canvas.get('startCorner','bottom-left').startswith('top')
                reverse=from_right ^ (canvas.get('order')=='serpentine' and band%2==1);x=width-1-offset if reverse else offset;y=band if from_top else height-1-band
            else:
                band=math.floor(index/height);offset=index%height;from_right=canvas.get('startCorner','bottom-left').endswith('right');from_top=canvas.get('startCorner','bottom-left').startswith('top')
                reverse=from_top ^ (canvas.get('order')=='serpentine' and band%2==1);y=offset if reverse else height-1-offset;x=width-1-band if from_right else band
            pixels.append((canvas['startPixel']+index,x,y))
    unique={address:(x,y) for address,x,y in pixels}
    vertical_groups={};horizontal_groups={}
    for address,(x,y) in unique.items():
        vertical_groups.setdefault(round(y),[]).append(address)
        horizontal_groups.setdefault(round(x),[]).append(address)
    top_down=[sorted(vertical_groups[key],key=lambda address:(unique[address][0],address)) for key in sorted(vertical_groups)]
    left_right=[sorted(horizontal_groups[key],key=lambda address:(unique[address][1],address)) for key in sorted(horizontal_groups)]
    return ([dict(addresses=strip,passIndex=0,step=index+1,total=len(top_down)) for index,strip in enumerate(top_down)]
            +[dict(addresses=strip,passIndex=1,step=index+1,total=len(left_right)) for index,strip in enumerate(left_right)])

def canvas_sweep_frame(frames,ip,output_ranges,canvas,addresses,color):
    """Overlay a one-pixel-thick strip without clearing other pixel sources."""
    frames={key:list(frame) for key,frame in frames.items()}
    order=canvas.get('colorOrder','BRG');universes=output_ranges[canvas['port']-1]
    universe_offset=canvas['firstUniverse']-universes[0]
    start=canvas['startPixel'];end=start+canvas['pixelCount']-1
    for pixel in range(start,end+1):
        for channel in range(3):
            absolute=(pixel-1)*3;universe_index=(absolute+channel)//512
            if universe_offset+universe_index<len(universes):frames.setdefault((ip,universes[universe_offset+universe_index]),[0]*512)
    if not addresses:return frames
    rgb={'R':255,'G':255,'B':255} if color=='white' else {'R':255,'G':0,'B':0}
    for address in addresses:
        absolute=(address-1)*3
        for channel,letter in enumerate(order):
            universe_index=(absolute+channel)//512
            if universe_offset+universe_index>=len(universes):raise ValueError('Canvas pixel is outside the controller output Universe range')
            frames.setdefault((ip,universes[universe_offset+universe_index]),[0]*512)[(absolute+channel)%512]=rgb[letter]
    return frames

def output_map_from_frames(ip,output_ranges,color_orders,frames):
    """Decode the last ArtDmx payload into per-port RGB pixels for the UI map."""
    ports=[]
    for port,universes in enumerate(output_ranges):
        count=min(SP801E_MAX_PIXELS_PER_PORT,len(universes)*512//3)
        pixels=[]
        for pixel in range(count):
            rgb={'R':0,'G':0,'B':0};order=color_orders[port]
            for channel in range(3):
                absolute=pixel*3+channel;universe_index=absolute//512
                if universe_index>=len(universes):continue
                frame=frames.get((ip,universes[universe_index]))
                if frame:rgb[order[channel]]=frame[absolute%512]
            pixels.append('#%02x%02x%02x'%(rgb['R'],rgb['G'],rgb['B']))
        ports.append(pixels)
    return ports

class LEDRuntime:
    def __init__(self):self.takeover=LastChange();self.render_modes={};self.manual={};self.last_active=set();self.status={};self.last_values={};self.locator=None;self.locator_suspended=False;self.locator_clear=None;self.output_test=None;self.output_map=None
    def set_values(self,f,values,enabled):
        if type(enabled) is not bool or not isinstance(values,list) or len(values)!=len(f['mapping']) or any(type(v) is not int or not 0<=v<=255 for v in values):raise ValueError('Invalid virtual DMX values')
        self.manual[f['id']]=dict(values=list(values),enabled=enabled,changed=time.monotonic())
    def values(self,fixtures,controls,monitor,ip,midi_controls=None,canvases=None):
        from input_controls import semantic_rules
        virtual=[virtual_fixture(f) for f in fixtures]
        matrix=next((f for f in fixtures if f['id']=='led:matrix'),None)
        canvas_targets=[c for c in (canvases or []) if matrix and c.get('controllerId')==matrix.get('controllerId') and c.get('enabled',True)]
        virtual.extend(virtual_fixture(matrix,canvas=c) for c in canvas_targets)
        by_id={f['id']:f for f in virtual};ids=set(by_id)
        result={k:list(v['values']) for k,v in self.manual.items() if v['enabled'] and k in ids}
        desk_times={('virtual',fid,channel['function']):self.manual.get(fid,{}).get('changed',0) for fid,f in by_id.items() for channel in f['channels'] if fid in result}
        with monitor.lock:
            for c in controls:
                targets=[fid for fid in c.get('targetFixtures',[]) if fid in ids]
                if not c.get('enabled') or not targets:continue
                try:baseline=monitor._fixture_baseline(ip,c['universe'])
                except ValueError:
                    if not any(o['controlId']==c['id'] for o in monitor.manual_overrides.values()):continue
                    baseline=monitor._optional_baseline(ip,c['universe'])
                rules=semantic_rules(targets,virtual,c.get('faderFunctions'))
                for fid in targets:result.setdefault(fid,[0]*len(by_id[fid]['channels']))
                for r in rules:
                    index=c['start']+r['input']-2
                    if index>=len(baseline):continue
                    override=monitor.manual_overrides.get((c['id'],r['input']))
                    value=override['value'] if override else baseline[index]
                    streams=[s for s in monitor.streams.values() if s['sourceIp']==ip and s['universe']==c['universe']]
                    stamp=override.get('changed',0) if override else streams[0].get('_changed',{}).get(index+1,0) if len(streams)==1 else 0
                    role=by_id[r['fixtureId']]['channels'][r['channel']-1]['function']
                    k=('virtual',r['fixtureId'],role);desk_times[k]=max(desk_times.get(k,0),stamp)
                    result[r['fixtureId']][r['channel']-1]=max(result[r['fixtureId']][r['channel']-1],value)
        desk={('virtual',fid,channel['function']):result[fid][i] for fid,f in by_id.items() if fid in result for i,channel in enumerate(f['channels'])}
        incoming=midi_mapped_values(midi_controls or [],virtual)
        mixed=self.takeover.mix(desk,incoming,desk_times)
        for (kind,fid,role),value in mixed.items():
            f=by_id[fid];roles=[channel['function'] for channel in f['channels']];result.setdefault(fid,[0]*len(roles))[roles.index(role)]=value
        # A matrix has one renderer. Select the mode belonging to the most
        # recently moved source; idle repeated DMX frames never change it.
        self.render_modes={}
        for f in fixtures:
            if f['id']!='led:matrix':continue
            for target_id in [f['id']]+[c['id'] for c in canvas_targets]:
                choices=[]
                manual=self.manual.get(target_id,{})
                if manual.get('enabled'):choices.append((manual['changed'],f.get('renderMode','pixel-fill')))
                for c in controls:
                    if not c.get('enabled') or target_id not in c.get('targetFixtures',[]):continue
                    with monitor.lock:
                        streams=[s for s in monitor.streams.values() if s['sourceIp']==ip and s['universe']==c['universe'] and time.monotonic()-s['_last']<10]
                        overrides=[o.get('changed',0) for (cid,_),o in monitor.manual_overrides.items() if cid==c['id']]
                        if not streams and not overrides:continue
                        stamps=[stamp for s in streams for channel,stamp in s.get('_changed',{}).items() if c['start']<=channel<c['start']+c.get('count',16)]
                    mode=c.get('targetModes',{}).get(target_id)
                    choices.append((max(stamps+overrides,default=0),mode if mode in ('parled','pixel-fill','screen') else f.get('renderMode','pixel-fill')))
                for c in midi_controls or []:
                    if not c.get('enabled') or target_id not in c.get('targetFixtures',[]) or not any(c['changed']):continue
                    mode=c.get('targetModes',{}).get(target_id)
                    choices.append((max(c['changed']),mode if mode in ('parled','pixel-fill','screen') else f.get('renderMode','pixel-fill')))
                self.render_modes[target_id]=max(choices,key=lambda choice:choice[0])[1] if choices else f.get('renderMode','pixel-fill')
        self.last_values=result
        return result
