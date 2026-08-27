from __future__ import annotations

from aiohttp import web

MAX_BULK_UPLOAD_BYTES = 25_000_000
MAX_BULK_PHONE_ENTRIES = 30_000_000


def validate_bulk_raw(raw: str) -> None:
    """Enforce the upload limit by UTF-8 byte size rather than Python characters."""
    size = len(str(raw or "").encode("utf-8"))
    if size > MAX_BULK_UPLOAD_BYTES:
        raise ValueError("bulk phone-number upload is too large (max 25 MB)")


def inject_bulk_file_uploads(text: str) -> str:
    """Add local TXT/CSV file pickers for caller-ID/random-number bulk inputs."""
    text = str(text or "")
    if 'id="pbx-bulk-file-upload-script"' in text:
        return text

    addon = r'''<style id="pbx-bulk-file-upload-style">
.pbxBulkFileRow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:7px 0}
.pbxBulkFileRow input[type=file]{width:auto;max-width:100%;min-height:38px;padding:6px 8px}
.pbxBulkFileRow .muted{font-size:10px}
</style>
<script id="pbx-bulk-file-upload-script">
(()=>{'use strict';
const MAX_BYTES=25000000;
const targets=[
  ['cidBulk','Caller ID numbers'],
  ['randomBulk','Random destination numbers'],
  ['cidBlockBulk','Caller ID blocks'],
  ['randomBlockBulk','Random destination blocks'],
];
function toast(msg,bad=false){const t=document.querySelector('#toast');if(!t){console[bad?'error':'log'](msg);return}t.textContent=msg;t.className='toast show'+(bad?' error':'');setTimeout(()=>{if(t.textContent===msg)t.className='toast'},4500)}
function wire(id,label){const area=document.getElementById(id);if(!area||area.dataset.fileUploadWired)return;area.dataset.fileUploadWired='1';const row=document.createElement('div');row.className='pbxBulkFileRow';const input=document.createElement('input');input.type='file';input.accept='.txt,.csv,text/plain,text/csv';input.setAttribute('aria-label',`Upload ${label}`);const hint=document.createElement('span');hint.className='muted';hint.textContent='TXT/CSV up to 25 MB';row.append(input,hint);area.insertAdjacentElement('afterend',row);input.addEventListener('change',async()=>{const file=input.files?.[0];if(!file)return;if(file.size>MAX_BYTES){toast(`File is ${(file.size/1000000).toFixed(1)} MB; maximum is 25 MB`,true);input.value='';return}try{area.value=await file.text();area.dispatchEvent(new Event('input',{bubbles:true}));toast(`Loaded ${file.name} · ${(file.size/1000000).toFixed(1)} MB`)}catch(e){toast(`Could not read ${file.name}: ${e.message}`,true);input.value=''}})}
function wireAll(){for(const [id,label] of targets)wire(id,label)}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',wireAll,{once:true});else wireAll();
new MutationObserver(wireAll).observe(document.documentElement,{childList:true,subtree:true});
})();
</script>'''
    return text.replace("</body>", addon + "</body>", 1) if "</body>" in text else text + addon


def apply() -> None:
    """Raise number-import ceilings and add file upload controls to the v3 console."""
    import webui_legacy
    import webui_v3

    # The v3 class inherits its bulk validators/payload formatter from the legacy
    # implementation. Patch the shared module globals and validator in one place.
    webui_legacy.MAX_BULK_PASTE_CHARS = MAX_BULK_UPLOAD_BYTES
    webui_legacy.MAX_BULK_PHONE_ENTRIES = MAX_BULK_PHONE_ENTRIES
    webui_legacy.WebControlServer._validate_bulk_raw = staticmethod(validate_bulk_raw)

    cls = webui_v3.WebControlServer
    if getattr(cls, "_bulk_uploads_applied", False):
        return

    original_index = cls.index

    async def index(self, request):
        response = await original_index(self, request)
        try:
            if getattr(response, "content_type", "") == "text/html" and response.text:
                response.text = inject_bulk_file_uploads(response.text)
                response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        except Exception:
            pass
        return response

    cls.index = index
    cls._bulk_uploads_applied = True
