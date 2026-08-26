from __future__ import annotations


MATRIX_UI_SCRIPT = r'''<style id="pbx-matrix-background-style">
html,body{background:#050806!important;background-image:none!important}
body::before,body::after{background-image:none!important}
#matrixRain{position:fixed;inset:0;width:100vw;height:100vh;z-index:0;pointer-events:none;opacity:.56}
body>*:not(#matrixRain){position:relative;z-index:1}
@media (prefers-reduced-motion:reduce){#matrixRain{opacity:.34}}
</style>
<canvas id="matrixRain" aria-hidden="true"></canvas>
<script id="pbx-matrix-background-script">
(()=>{'use strict';
const canvas=document.getElementById('matrixRain');if(!canvas)return;
const ctx=canvas.getContext('2d',{alpha:true});
const glyphs='01ABCDEFGHIJKLMNOPQRSTUVWXYZ<>[]{}#$%&*+-=/\\\\';
let width=0,height=0,dpr=1,fontSize=15,columns=0,drops=[],raf=0,last=0;
const reduced=window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches;
function resize(){
  dpr=Math.min(window.devicePixelRatio||1,1.5);
  width=window.innerWidth;height=window.innerHeight;
  canvas.width=Math.max(1,Math.floor(width*dpr));canvas.height=Math.max(1,Math.floor(height*dpr));
  canvas.style.width=width+'px';canvas.style.height=height+'px';
  ctx.setTransform(dpr,0,0,dpr,0,0);
  fontSize=width<760?13:15;columns=Math.ceil(width/fontSize);
  drops=Array.from({length:columns},()=>Math.floor(Math.random()*-80));
  ctx.fillStyle='#050806';ctx.fillRect(0,0,width,height);
}
function glyph(){return glyphs[Math.floor(Math.random()*glyphs.length)]}
function frame(ts){
  if(ts-last<(width<760?52:38)){raf=requestAnimationFrame(frame);return}
  last=ts;
  ctx.fillStyle='rgba(5,8,6,.105)';ctx.fillRect(0,0,width,height);
  ctx.font=`600 ${fontSize}px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace`;
  ctx.textAlign='center';ctx.textBaseline='top';
  for(let i=0;i<columns;i++){
    const y=drops[i]*fontSize;
    ctx.fillStyle=Math.random()>.94?'rgba(190,255,211,.88)':'rgba(70,235,126,.46)';
    ctx.fillText(glyph(),i*fontSize+fontSize/2,y);
    if(y>height&&Math.random()>.972)drops[i]=Math.floor(Math.random()*-25);
    else drops[i]++;
  }
  raf=requestAnimationFrame(frame);
}
function staticFrame(){
  ctx.fillStyle='#050806';ctx.fillRect(0,0,width,height);
  ctx.font=`600 ${fontSize}px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace`;
  ctx.textAlign='center';ctx.textBaseline='top';
  for(let x=0;x<columns;x++){
    for(let y=Math.floor(Math.random()*5);y<Math.ceil(height/fontSize);y+=Math.floor(Math.random()*5)+4){
      ctx.fillStyle='rgba(70,235,126,.22)';
      ctx.fillText(glyph(),x*fontSize+fontSize/2,y*fontSize);
    }
  }
}
resize();
if(reduced)staticFrame();else raf=requestAnimationFrame(frame);
let timer=0;window.addEventListener('resize',()=>{clearTimeout(timer);timer=setTimeout(()=>{cancelAnimationFrame(raf);resize();if(reduced)staticFrame();else raf=requestAnimationFrame(frame)},120)},{passive:true});
})();
</script>'''


def inject_matrix_ui(html: str) -> str:
    if 'id="pbx-matrix-background-script"' in html:
        return html
    if "</body>" in html:
        return html.replace("</body>", MATRIX_UI_SCRIPT + "</body>", 1)
    return html + MATRIX_UI_SCRIPT


def apply() -> None:
    import webui_v3

    cls = webui_v3.WebControlServer
    if getattr(cls, "_matrix_background_applied", False):
        return

    for name in ("index", "login_page", "setup_page"):
        original = getattr(cls, name)

        async def wrapped(self, request, _original=original):
            response = await _original(self, request)
            try:
                if getattr(response, "content_type", "") == "text/html" and response.text:
                    response.text = inject_matrix_ui(response.text)
                    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            except Exception:
                pass
            return response

        setattr(cls, name, wrapped)

    cls._matrix_background_applied = True
