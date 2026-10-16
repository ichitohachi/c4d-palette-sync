"""Cinema 4D QC4DLAY6 palette addition tool (experimental, tested on supplied files).

Run in C4D Script Manager for checkbox selection, or with Python:
  python c4d_palette_sync.py custom.l4d standard.l4d --report report.json
  python c4d_palette_sync.py custom.l4d standard.l4d --output updated.l4d

Only adds missing entries to confidently matched palettes. Never removes/reorders
existing entries. Unknown formats fail closed. A new output file is required.
"""
import argparse
import copy
import json
import re
import struct
import sys
import types
import traceback
import zipfile
from datetime import datetime
from pathlib import Path


class LayoutError(ValueError):
    pass


class Reader:
    FORMATS = {11:'b',12:'B',13:'h',14:'H',15:'i',16:'I',17:'q',18:'Q',
               19:'d',20:'d',21:'B',29:'f'}

    def __init__(self, data):
        if data[:8] != b'QC4DLAY6' or len(data) < 14:
            raise LayoutError('Unsupported layout format')
        self.data, self.pos = data, 13

    def take(self, n):
        if n < 0 or self.pos+n > len(self.data):
            raise LayoutError('Truncated or invalid layout')
        result = self.data[self.pos:self.pos+n]
        self.pos += n
        return result

    def unpack(self, fmt):
        return struct.unpack('>'+fmt, self.take(struct.calcsize('>'+fmt)))[0]

    def value(self):
        t = self.unpack('B')
        if t == 1:
            ident, level = self.unpack('i'), self.unpack('i')
            if ident != 132 or level != 1:
                raise LayoutError('Unsupported chunk')
            values = []
            while self.pos < len(self.data) and self.data[self.pos] != 2:
                values.append(self.value())
            if self.take(1) != b'\x02':
                raise LayoutError('Missing chunk end')
            return {'chunk':ident, 'level':level, 'values':values}
        if t in self.FORMATS:
            return self.unpack(self.FORMATS[t])
        if t == 130:
            return self.take(self.unpack('i')).decode('utf-16-be')
        raise LayoutError('Unsupported data type: '+str(t))


def entries(c):
    v = c['values']
    if len(v) % 3 != 1:
        raise LayoutError('Invalid container')
    return list(zip(v[1::3], v[2::3], v[3::3]))


def get(c, key, default=None):
    return next((v for k,t,v in entries(c) if k == key), default)


def encode(t, v):
    if t == 132:
        body = encode(15, v['values'][0])
        for k, dtype, value in entries(v):
            body += encode(15,k) + encode(15,dtype) + encode(dtype,value)
        return b'\x01' + struct.pack('>ii',132,1) + body + b'\x02'
    if t == 130:
        s = v.encode('utf-16-be')
        return bytes([t]) + struct.pack('>i',len(s)) + s
    if t in Reader.FORMATS:
        return bytes([t]) + struct.pack('>'+Reader.FORMATS[t],v)
    raise LayoutError('Unsupported output type: '+str(t))


def parse(data):
    r = Reader(data)
    root = r.value()
    if r.pos != len(data):
        raise LayoutError('Unparsed trailing data')
    # Exact round trip is required before any edits are considered.
    if data[:13] + encode(132,root) != data:
        raise LayoutError('Byte-for-byte round trip failed')
    return root


def walk(c, path=()):
    yield path,c
    for k,t,v in entries(c):
        if t == 132:
            yield from walk(v, path+(k,))


ASSET_LABELS = {
    'net.maxon.neutron.node.primitive.curve.line': '直線スプライン（Segment）',
    'com.rocketlasso.neutron.asset.fui.wavy': '波形スプライン（Wave）',
    'com.rocketlasso.neutron.asset.modifier.branch': '分岐 スプライン モディファイア',
    'com.rocketlasso.neutron.asset.break.modifier': '破断 スプライン モディファイア',
    'com.rocketlasso.neutron.asset.modifier.catenary': 'カテナリー スプライン モディファイア',
    'com.rocketlasso.neutron.asset.modifier.electric': '放電 スプライン モディファイア',
    'com.rocketlasso.neutron.asset.pulse.modifier': 'パルス スプライン モディファイア',
    'com.rocketlasso.neutron.asset.modifier.dash': '破線 スプライン モディファイア',
    'com.rocketlasso.neutron.asset.modifier.partition': 'パーティション モディファイア',
}


def command_identity(c):
    if get(c,2) not in (1017,1029): return None
    # Asset-backed buttons have numeric command ID 0 and a string ID in 1056.
    asset = get(c,1056)
    if asset: return asset
    command = get(c,4)
    return command if isinstance(command,int) and command > 0 else None


def commands(c):
    return {identity for _,v in walk(c)
            if (identity := command_identity(v)) is not None}


def palettes(root):
    # Root key 1 is the main layout tree in the tested files. Key 38 contains
    # saved manager layouts; key 45 contains context variants. Do not modify them.
    return [(p,c) for p,c in walk(root)
            if p and p[0] == 1 and 45 not in p and get(c,2) == 1018]


def item_key(c):
    identity = command_identity(c)
    if identity is not None:
        return get(c,2),identity
    return None


class Plan:
    def __init__(self, custom_data, standard_data, names=None):
        self.data = custom_data
        self.root, self.standard = parse(custom_data), parse(standard_data)
        self.names = names or {}
        self.additions, self.matches, self.unmatched = [], [], []
        standards = palettes(self.standard)
        for path,c in palettes(self.root):
            ids = commands(c)
            ranked = sorted([(len(ids & commands(s))/max(1,len(ids | commands(s))),i,p,s)
                             for i,(p,s) in enumerate(standards)], reverse=True)
            score,i,sp,s = ranked[0] if ranked else (0,0,(),None)
            runner_up = ranked[1][0] if len(ranked)>1 else 0
            if score < .65 or score-runner_up < .10:
                self.unmatched.append({'path':path,'title':get(c,15,''),'best_score':score})
                continue
            self.matches.append({'custom_path':path,'standard_path':sp,'score':score,
                                 'custom_only_ids':sorted(ids-commands(s),key=str)})
            self.compare(c,s,path, get(c,15,'Palette'))

    def label(self, cmd):
        return self.names.get(str(cmd)) or ASSET_LABELS.get(cmd) or 'Command '+str(cmd)

    def compare(self, custom, standard, path, label):
        a,b = get(custom,1),get(standard,1)
        if not isinstance(a,dict) or not isinstance(b,dict): return
        ae,be = entries(a),entries(b)
        # All tested item lists are contiguous, one-based containers.
        if [k for k,t,v in ae] != list(range(1,len(ae)+1)): return
        if any(t!=132 for k,t,v in ae+be): return
        amap = {}
        for k,t,v in ae:
            if item_key(v): amap.setdefault(item_key(v),[]).append((k,v))
        all_ids = commands(custom)
        for index,(k,t,v) in enumerate(be):
            key = item_key(v)
            if not key: continue
            if key in amap:
                if key[0]==1029 and len(amap[key])==1:
                    ak,av = amap[key][0]
                    self.compare(av,v,path+(1,ak),label+' / '+self.label(key[1]))
            elif key[1] not in all_ids:
                anchors = [item_key(x) for _,_,x in be[:index] if item_key(x)]
                after = [item_key(x) for _,_,x in be[index+1:] if item_key(x)]
                if not any(x in amap for x in anchors+after): continue
                self.additions.append({'id':len(self.additions),'command_id':key[1],
                    'name':self.label(key[1]),'group':label,'path':path,
                    'target':a,'item':v,'before_keys':anchors,'after_keys':after})

    def report(self):
        return {'status':'candidate additions; old baseline unavailable',
                'scope':'main layout palettes and folded groups; context variants excluded',
                'additions':[{k:v for k,v in a.items() if k not in
                              ('target','item','before_keys','after_keys')} for a in self.additions],
                'matches':self.matches,'unmatched':self.unmatched}

    def write(self, output, selected):
        output = Path(output)
        if output.exists(): raise LayoutError('Output already exists; choose a new filename')
        for a in self.additions:
            if a['id'] not in selected: continue
            target = a['target']; rows = entries(target)
            keys = [item_key(v) for k,t,v in rows]
            pos = None
            for anchor in reversed(a['before_keys']):
                if anchor in keys:
                    pos = keys.index(anchor)+1; break
            if pos is None:
                for anchor in a['after_keys']:
                    if anchor in keys:
                        pos=keys.index(anchor); break
            if pos is None: raise LayoutError('Insertion anchor disappeared')
            rows.insert(pos,(0,132,copy.deepcopy(a['item'])))
            target['values'] = [target['values'][0]]
            for i,(_,t,v) in enumerate(rows,1): target['values'].extend((i,t,v))
        data = self.data[:13]+encode(132,self.root)
        parse(data)
        output.parent.mkdir(parents=True,exist_ok=True)
        with output.open('xb') as f: f.write(data)


_dialog = None


def keep_dialog(dialog):
    # Script Manager may release its execution globals when a script returns.
    # Keep async dialogs in a persistent module, as a loaded plugin would.
    registry = sys.modules.setdefault('_c4d_palette_sync_windows',
                                      types.ModuleType('_c4d_palette_sync_windows'))
    previous = getattr(registry, 'dialog', None)
    if previous is not None and previous is not dialog and previous.IsOpen():
        previous.Close()
    registry.dialog = dialog


def show_error(message):
    import c4d
    class ErrorDialog(c4d.gui.GeDialog):
        def CreateLayout(self):
            self.SetTitle('パレット更新：エラー')
            for i,line in enumerate(str(message).splitlines()):
                self.AddStaticText(100+i,c4d.BFH_LEFT,name=line)
            self.AddButton(20,c4d.BFH_LEFT,name='更新するレイアウト（.l4d）を選び直す')
            return True
        def Command(self,id,msg):
            if id == 20:
                try:
                    cinema_main()
                except Exception as exc:
                    traceback.print_exc()
                    show_error(type(exc).__name__ + ': ' + str(exc))
            return True
    dialog = ErrorDialog()
    keep_dialog(dialog)
    dialog.Open(c4d.DLG_TYPE_ASYNC,defaultw=740,defaulth=160)


def show_launcher():
    import c4d
    class Launcher(c4d.gui.GeDialog):
        def CreateLayout(self):
            self.SetTitle('カスタムUIのパレット更新')
            self.AddStaticText(10,c4d.BFH_LEFT,name='更新するカスタムUIのレイアウト（.l4d）を選択してください。')
            self.AddButton(20,c4d.BFH_LEFT,name='レイアウトを選択して比較')
            return True
        def Command(self,id,msg):
            if id == 20:
                try:
                    filename = c4d.storage.LoadDialog(
                        title='更新するカスタムUI（.l4d）を選択',
                        def_path=str(Path(c4d.storage.GeGetStartupWritePath())/'library'/'layout'),
                        def_file='normal.l4d')
                    if filename:
                        cinema_main(filename)
                except Exception as exc:
                    traceback.print_exc()
                    show_error(type(exc).__name__ + ': ' + str(exc))
            return True
    dialog = Launcher()
    keep_dialog(dialog)
    if not dialog.Open(c4d.DLG_TYPE_ASYNC,defaultw=620,defaulth=150):
        raise LayoutError('起動画面を開けませんでした。')


def cinema_main(filename=None):
    import c4d
    global _dialog
    if filename is None:
        show_launcher()
        return
    if not filename: return
    source = Path(filename)
    if source.suffix.lower() != '.l4d':
        raise LayoutError('選択したファイル: ' + str(source) + '\n'
                          'ここでは更新対象のレイアウト（.l4d）を選んでください。\n'
                          '.py はスクリプト本体で、更新対象ではありません。')
    custom_data = source.read_bytes()
    try:
        parse(custom_data)
    except (LayoutError, ValueError, RecursionError) as exc:
        raise LayoutError('レイアウトを読み取れません: ' + str(source) + '\n'
                          '先頭8バイト: ' + repr(custom_data[:8]) + '\n' + str(exc)) from exc
    library = Path(c4d.storage.GeGetStartupPath())/'library.zip'
    with zipfile.ZipFile(library) as z:
        standard = z.read('layout/new/#$00Standard.l4d')
    try:
        standard_root = parse(standard)
    except (LayoutError, ValueError, RecursionError) as exc:
        raise LayoutError('C4D標準レイアウトを読み取れません: ' + str(library) + '\n' + str(exc)) from exc
    names = {str(i):c4d.GetCommandName(i) for i in commands(standard_root) if isinstance(i,int)}
    plan = Plan(custom_data,standard,names)

    show_review(plan, filename)


def default_save_name(source_path, version=None):
    if version is None:
        import c4d
        version = c4d.GetC4DVersion()
    version_suffix = f'{version // 1000}.{version % 1000 // 100}.{version % 100}'
    stem = Path(source_path).stem
    # Replace previous version/date suffixes, including repeated suffixes.
    while True:
        match = re.search(r'_20\d{2}(?:\.\d+){0,2}$', stem)
        if match:
            stem = stem[:match.start()]
            continue
        match = re.search(r'_(\d{6})$', stem)
        if not match: break
        try:
            datetime.strptime(match.group(1), '%y%m%d')
        except ValueError:
            break
        stem = stem[:match.start()]
    return f'{stem}_{version_suffix}.l4d'


def show_review(plan, source_path):
    import c4d
    global _dialog
    source_path = Path(source_path).resolve()
    class Review(c4d.gui.GeDialog):
        def CreateLayout(self):
            self.SetTitle('パレット更新：追加候補の確認')
            self.AddStaticText(10,c4d.BFH_LEFT,name='既存項目の順序と配置を保持し、選択した項目を追加します。')
            self.AddStaticText(11,c4d.BFH_LEFT,name='不足項目の候補です。新機能か、意図的な除外かは判定できません。')
            for a in plan.additions:
                self.AddCheckbox(100+a['id'],c4d.BFH_LEFT,initw=0,inith=0,name=a['group']+' : '+a['name'])
            self.AddButton(20,c4d.BFH_LEFT,name='更新版レイアウトを別名保存')
            self.AddStaticText(21,c4d.BFH_LEFT,name=str(len(plan.additions))+' 件の追加候補')
            return True
        def InitValues(self):
            for a in plan.additions: self.SetBool(100+a['id'],True)
            return True
        def Command(self,id,msg):
            if id==20:
                output=c4d.storage.SaveDialog(
                    title='Save updated layout',force_suffix='l4d',
                    def_path=str(source_path.parent),
                    def_file=default_save_name(source_path))
                if output:
                    try:
                        selected={a['id'] for a in plan.additions if self.GetBool(100+a['id'])}
                        copy.deepcopy(plan).write(output,selected)
                        self.SetString(21,'保存済み: '+output)
                        self.Enable(20,False)
                    except Exception as exc:
                        self.SetString(21,str(exc))
            return True
    _dialog=Review()
    keep_dialog(_dialog)
    if not _dialog.Open(c4d.DLG_TYPE_ASYNC,defaultw=740,defaulth=420):
        raise LayoutError('確認画面を開けませんでした。')


def cli_main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('custom');p.add_argument('standard')
    p.add_argument('--names');p.add_argument('--report');p.add_argument('--output')
    p.add_argument('--select',help='Comma-separated candidate numbers; defaults to all')
    a=p.parse_args()
    names=json.loads(Path(a.names).read_text(encoding='utf-8')) if a.names else {}
    plan=Plan(Path(a.custom).read_bytes(),Path(a.standard).read_bytes(),names)
    text=json.dumps(plan.report(),ensure_ascii=False,indent=2)
    if a.report: Path(a.report).write_text(text,encoding='utf-8')
    print(json.dumps({'candidates':len(plan.additions),'matched_palettes':len(plan.matches),
                      'unmatched_palettes':len(plan.unmatched)}))
    if a.output:
        selected={int(n) for n in a.select.split(',')} if a.select else {v['id'] for v in plan.additions}
        plan.write(a.output,selected)


if __name__=='__main__':
    try:
        import c4d
    except ImportError:
        cli_main()
    else:
        try:
            cinema_main()
        except Exception as exc:
            traceback.print_exc()
            show_error(type(exc).__name__ + ': ' + str(exc))
