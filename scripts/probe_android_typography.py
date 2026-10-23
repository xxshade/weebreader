"""Serve a disposable Firefox Android typography experiment using reader CSS.

Open / on the emulator at http://10.0.2.2:8765. The page posts its measurements
back; --output records evidence. A baseline with the correction removed must
reproduce inflation, while production CSS must preserve book sizes and reader
font scaling. Desktop browser measurements are not Android validation.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def document():
    source = (ROOT / 'app/static/js/reader-text.js').read_text()
    theme = re.search(r'    function themeCssText\(\) \{.*?\n    \}', source, re.S).group()
    return '''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reader typography regression</title><pre id="result">Measuring…</pre><script>
const THEMES={light:{background:'#fff',color:'#000'}};
const prefs={theme:'light',font:'original',fontSize:100,lineHeight:1.6,width:720};
''' + theme + '''
(async()=>{
 const result={userAgent:navigator.userAgent,cases:[],errors:[]};
 for (const baseline of [true,false]) for (const scale of [100,150]) {
  prefs.fontSize=scale;
  const f=document.createElement('iframe'); f.style.cssText='width:360px;height:480px;border:0';f.sandbox='allow-same-origin';
  const css=baseline?themeCssText().replace('-moz-text-size-adjust: none;',''):themeCssText();
  f.srcdoc='<html><head><link rel="stylesheet" href="/app.css"><style>'+css+'p{display:flow-root}h2{font-size:2em}</style></head><body><p id="short">MMMMMMMMMM</p><p id="long">'+('MMMMMMMMMM '.repeat(100))+'</p><h2 id="heading">MMMMMMMMMM</h2></body></html>';
  const loaded=new Promise(resolve=>f.onload=resolve); document.body.append(f);await loaded;
  const d=f.contentDocument;d.body.getBoundingClientRect();await d.fonts.ready;
  await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  const measure=id=>{const p=d.getElementById(id),range=d.createRange();range.setStart(p.firstChild,0);range.setEnd(p.firstChild,10);const r=range.getBoundingClientRect();return {font:d.defaultView.getComputedStyle(p).fontSize,width:r.width,height:r.height}};
  result.cases.push({baseline,scale,short:measure('short'),long:measure('long'),heading:measure('heading')});f.remove();
 }
 const [old,oldLarge,fixed,large]=result.cases;
 if (/Android/.test(navigator.userAgent) && Math.abs(old.short.width-old.long.width)<1) result.errors.push('Baseline did not reproduce uneven inflation');
 if (Math.abs(fixed.short.width-fixed.long.width)>1) result.errors.push('Paragraphs with the same font render at different sizes');
 if (fixed.heading.width<fixed.short.width*1.9) result.errors.push('Original heading size lost');
 if (large.short.width<fixed.short.width*1.4) result.errors.push('Reader font scaling lost');
 document.getElementById('result').textContent=JSON.stringify(result,null,2);
 await fetch('/result',{method:'POST',body:JSON.stringify(result)});
})().catch(error=>fetch('/result',{method:'POST',body:JSON.stringify({errors:[String(error)]})}));
</script>'''


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/app.css':
                content = (ROOT / 'app/static/src/app.css').read_bytes()
                kind = 'text/css'
            elif self.path.startswith('/fonts/') and '..' not in self.path:
                file = ROOT / 'app/static/src' / self.path.lstrip('/')
                if not file.is_file():
                    self.send_error(404)
                    return
                content = file.read_bytes()
                kind = 'font/woff2'
            else:
                content = document().encode()
                kind = 'text/html'
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            result = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            args.output.write_text(json.dumps(result, indent=2) + '\n')
            print('Typography probe:', 'FAIL' if result.get('errors') else 'PASS', flush=True)
            self.send_response(204)
            self.end_headers()

    ThreadingHTTPServer(('0.0.0.0', args.port), Handler).serve_forever()
