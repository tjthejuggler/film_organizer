"""Device pairing: pair page, code redemption, QR + device management."""
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from .. import config, pairing

router = APIRouter()


# Self-contained pair page served to unpaired remote devices (no template
# engine, no static dependency — this one string is the whole onboarding UI).
PAIR_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Film Organizer — pair device</title>
<style>
 body{font-family:system-ui,sans-serif;background:#14161b;color:#e8e6e1;
      display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
 .card{background:#1d2027;padding:2rem;border-radius:12px;max-width:24rem;
       width:90%;box-shadow:0 8px 30px rgba(0,0,0,.5)}
 h1{font-size:1.2rem;margin:0 0 .5rem}
 p{color:#9aa0ab;font-size:.9rem;line-height:1.5}
 input{width:100%;box-sizing:border-box;font-size:1.4rem;letter-spacing:.4em;
       text-align:center;padding:.6rem;border-radius:8px;
       border:1px solid #333a46;background:#14161b;color:#e8e6e1;margin:.8rem 0}
 button{width:100%;padding:.7rem;border:0;border-radius:8px;font-size:1rem;
        background:#4f8cff;color:#fff;font-weight:600;cursor:pointer}
 .err{color:#ff7b72;font-size:.85rem;min-height:1.2em}
</style></head><body><div class="card">
<h1>🎬 Film Organizer</h1>
<p>Enter the 6-digit code shown in <b>Settings → Devices</b> on the
computer running Film Organizer.</p>
<input id="code" inputmode="numeric" maxlength="6" placeholder="000000"
       autofocus autocomplete="one-time-code">
<div class="err" id="err"></div>
<button onclick="go()">Pair this device</button>
</div><script>
const pre=new URLSearchParams(location.search).get("code");
if(pre)document.getElementById("code").value=pre;
async function go(){
 const err=document.getElementById("err");err.textContent="";
 try{
  const r=await fetch("/api/pair",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({code:document.getElementById("code").value.trim()})});
  if(!r.ok){const d=await r.json().catch(()=>({}));
    throw new Error(d.detail||("HTTP "+r.status));}
  location.href="/";
 }catch(e){err.textContent=e.message;}
}
document.getElementById("code").addEventListener("keydown",
 e=>{if(e.key==="Enter")go();});
</script></body></html>"""


@router.get("/pair")
def pair_form():
    """Phone-friendly page: asks for the 6-digit code (pre-filled when the
    QR link was scanned) and stores the device cookie on success."""
    return Response(PAIR_PAGE, media_type="text/html")


@router.post("/api/pair")
def pair_submit(body: dict):
    code = str((body or {}).get("code", "")).strip()
    try:
        granted = pairing.redeem(code)
    except ValueError as e:
        raise HTTPException(400, str(e))
    resp = JSONResponse({"ok": True})
    resp.set_cookie(pairing.COOKIE_NAME, granted["token"],
                    max_age=pairing.TOKEN_TTL, httponly=True,
                    samesite="lax", path="/")
    return resp


# ---- pairing management (Settings sheet; loopback-only via the gate) -------
@router.get("/api/pairing/info")
def pairing_info():
    """Current code + expiry + paired devices + LAN URL for the QR."""
    info = pairing.peek_code()
    urls = pairing.lan_urls()
    return {"code": info["code"], "expires_in": info["expires_in"],
            "lan_url": urls[0] if urls else "",
            "devices": pairing.list_devices()}


@router.get("/api/pairing/qr.svg")
def pairing_qr_svg():
    """QR code (pure-SVG, no extra deps) encoding the LAN pair URL."""
    from .. import qrsvg
    info = pairing.peek_code()
    base = pairing.lan_urls() or [f"http://127.0.0.1:{config.PORT}/"]
    url = f"{base[0].rstrip('/')}/pair?code={info['code']}"
    return Response(qrsvg.encode(url), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


@router.post("/api/pairing/regenerate")
def pairing_regenerate():
    info = pairing.new_code()
    return {"ok": True, "code": info, "expires_in": pairing.CODE_TTL}


class DeviceRenameIn(BaseModel):
    name: str


@router.post("/api/pairing/devices/{device_id}/rename")
def pairing_rename(device_id: int, body: DeviceRenameIn):
    pairing.rename_device(device_id, body.name.strip() or "Device")
    return {"ok": True}


@router.delete("/api/pairing/devices/{device_id}")
def pairing_revoke(device_id: int):
    if not pairing.revoke_device(device_id):
        raise HTTPException(404, "no such device")
    return {"ok": True}
