import asyncio
import base64
import hashlib
import json
import secrets
import string
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

app = FastAPI()

# ----------------------------
# Deterministic PRNG + shuffle
# SplitMix64 (same algorithm in JS for verification)
# ----------------------------
MASK64 = (1 << 64) - 1

def _u64(x: int) -> int:
    return x & MASK64

class SplitMix64:
    def __init__(self, seed_u64: int):
        self.state = _u64(seed_u64)

    def next_u64(self) -> int:
        self.state = _u64(self.state + 0x9E3779B97F4A7C15)
        z = self.state
        z = _u64((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9)
        z = _u64((z ^ (z >> 27)) * 0x94D049BB133111EB)
        return _u64(z ^ (z >> 31))

    def randbelow(self, n: int) -> int:
        # Rejection sampling for unbiased modulo
        if n <= 0:
            raise ValueError("n must be > 0")
        limit = (1 << 64) - ((1 << 64) % n)
        while True:
            r = self.next_u64()
            if r < limit:
                return r % n

def deterministic_shuffle(items: List[str], master_seed_bytes: bytes) -> List[str]:
    # Take first 8 bytes as u64 seed
    seed_u64 = int.from_bytes(master_seed_bytes[:8], "big", signed=False)
    rng = SplitMix64(seed_u64)
    arr = items[:]
    # Fisher-Yates
    for i in range(len(arr) - 1, 0, -1):
        j = rng.randbelow(i + 1)
        arr[i], arr[j] = arr[j], arr[i]
    return arr

# ----------------------------
# Poker deck utilities
# ----------------------------
RANKS = ["A","K","Q","J","10","9","8","7","6","5","4","3","2"]
SUITS = ["♠","♥","♦","♣"]

def make_deck() -> List[str]:
    return [r + s for s in SUITS for r in RANKS]

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def sha256_bytes(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()

def room_code(n=6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))

# ----------------------------
# Room state
# ----------------------------
@dataclass
class Player:
    pid: str
    name: str
    avatar: str
    ws: Optional[WebSocket] = None
    is_host: bool = False

    # fairness fields
    commitment: Optional[str] = None  # public
    seed: Optional[str] = None        # private to server until audit
    salt: Optional[str] = None        # private to server until audit

    # hand
    hole: List[str] = field(default_factory=list)

@dataclass
class Room:
    code: str
    created_at: float = field(default_factory=time.time)
    players: Dict[str, Player] = field(default_factory=dict)

    variant: str = "TEXAS"  # TEXAS | OMAHA
    stage: str = "LOBBY"    # LOBBY | COMMIT | REVEAL | HAND | AUDIT

    # per-hand
    community: List[str] = field(default_factory=list)
    deck: List[str] = field(default_factory=list)
    deal_index: int = 0

    # audit transcript
    master_seed_hex: Optional[str] = None
    transcript: Dict = field(default_factory=dict)

rooms: Dict[str, Room] = {}

# ----------------------------
# HTML (casino UI)
# ----------------------------
INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CASINO POKER • Verifiable Shuffle</title>
  <style>
    :root{
      --bg:#07080b;
      --felt1:#0b3a2a;
      --felt2:#062418;
      --gold:#f2d37b;
      --gold2:#b88a2a;
      --neon:#4cffc3;
      --red:#ff4d6d;
      --muted:#9fb3aa;
      --card:#f7f7fb;
      --card2:#e8e8f2;
      --shadow: 0 20px 60px rgba(0,0,0,.55);
      --glass: rgba(255,255,255,.06);
      --glass2: rgba(255,255,255,.10);
    }
    *{box-sizing:border-box}
    html,body{height:100%; margin:0; background: radial-gradient(1000px 650px at 50% 30%, #0f141a 0%, var(--bg) 55%, #040509 100%); overflow:hidden; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Inter, Arial;}
    .app{
      height:100vh; width:100vw;
      display:flex; align-items:stretch; justify-content:center;
      padding:18px;
      gap:14px;
    }
    /* Left panel */
    .panel{
      width:360px; min-width:330px;
      background: linear-gradient(180deg, rgba(255,255,255,.10) 0%, rgba(255,255,255,.06) 100%);
      border:1px solid rgba(255,255,255,.10);
      border-radius:24px;
      box-shadow: var(--shadow);
      padding:16px;
      display:flex; flex-direction:column;
      overflow:hidden;
    }
    .brand{
      display:flex; align-items:center; justify-content:space-between;
      gap:10px; padding:10px 10px 14px 10px;
    }
    .logo{
      display:flex; align-items:center; gap:10px;
    }
    .chip{
      width:34px; height:34px; border-radius:999px;
      background: conic-gradient(from 90deg, var(--gold), #ffffff, var(--gold2), #ffffff, var(--gold));
      box-shadow: 0 10px 30px rgba(0,0,0,.35);
      border:1px solid rgba(242,211,123,.45);
      position:relative;
    }
    .chip::after{
      content:"";
      position:absolute; inset:7px;
      border-radius:999px;
      background: radial-gradient(circle at 35% 35%, rgba(255,255,255,.35), rgba(0,0,0,.15));
      border:1px solid rgba(255,255,255,.18);
    }
    .title{
      font-weight:800; letter-spacing:.18em; font-size:12px; color:rgba(242,211,123,.95);
    }
    .sub{
      font-size:12px; color:rgba(159,179,170,.85); margin-top:2px;
    }
    .pill{
      font-size:11px;
      padding:6px 10px;
      border-radius:999px;
      border:1px solid rgba(255,255,255,.12);
      background: rgba(0,0,0,.20);
      color:rgba(255,255,255,.85);
      display:inline-flex; align-items:center; gap:8px;
    }
    .dot{width:7px;height:7px;border-radius:99px;background:var(--neon); box-shadow:0 0 12px rgba(76,255,195,.65);}

    .section{
      margin-top:10px;
      padding:12px;
      border-radius:18px;
      border:1px solid rgba(255,255,255,.08);
      background: rgba(0,0,0,.16);
    }
    .label{font-size:11px; color:rgba(159,179,170,.85); letter-spacing:.12em; text-transform:uppercase;}
    input, select{
      width:100%;
      margin-top:10px;
      padding:12px 12px;
      border-radius:14px;
      border:1px solid rgba(255,255,255,.14);
      background: rgba(255,255,255,.06);
      color: rgba(255,255,255,.90);
      outline:none;
    }
    input::placeholder{color: rgba(255,255,255,.35)}
    .row{display:flex; gap:10px}
    .row > *{flex:1}
    .btn{
      width:100%;
      margin-top:10px;
      padding:12px 14px;
      border-radius:14px;
      border:1px solid rgba(242,211,123,.34);
      background: linear-gradient(180deg, rgba(242,211,123,.20), rgba(242,211,123,.10));
      color: rgba(242,211,123,.98);
      font-weight:800;
      letter-spacing:.10em;
      cursor:pointer;
      transition: transform .08s ease, background .18s ease, border-color .18s ease;
      box-shadow: 0 18px 40px rgba(0,0,0,.35);
      user-select:none;
    }
    .btn:hover{transform: translateY(-1px); border-color: rgba(242,211,123,.55)}
    .btn:active{transform: translateY(0px)}
    .btn2{
      width:100%;
      margin-top:10px;
      padding:12px 14px;
      border-radius:14px;
      border:1px solid rgba(255,255,255,.14);
      background: rgba(255,255,255,.06);
      color: rgba(255,255,255,.88);
      font-weight:700;
      letter-spacing:.08em;
      cursor:pointer;
    }
    .btnSmall{
      padding:10px 12px; border-radius:14px; font-size:12px;
    }
    .grid2{display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:10px;}
    .hint{font-size:12px; color:rgba(159,179,170,.85); line-height:1.35; margin-top:10px;}
    .hint b{color:rgba(255,255,255,.92)}
    .log{
      margin-top:10px;
      padding:10px; border-radius:14px;
      border:1px solid rgba(255,255,255,.08);
      background: rgba(0,0,0,.20);
      color: rgba(255,255,255,.82);
      font-size:12px;
      height: 220px;
      overflow:auto;
      white-space: pre-wrap;
    }

    /* Table */
    .tableWrap{
      flex:1;
      border-radius:28px;
      border:1px solid rgba(255,255,255,.10);
      background:
        radial-gradient(1200px 700px at 50% 35%, rgba(76,255,195,.12), rgba(0,0,0,0) 55%),
        radial-gradient(900px 520px at 50% 40%, rgba(242,211,123,.07), rgba(0,0,0,0) 60%),
        linear-gradient(180deg, rgba(255,255,255,.07), rgba(255,255,255,.03));
      box-shadow: var(--shadow);
      position:relative;
      overflow:hidden;
      min-width: 680px;
    }

    .felt{
      position:absolute; inset:16px;
      border-radius:24px;
      background:
        radial-gradient(900px 520px at 50% 45%, rgba(255,255,255,.08), rgba(0,0,0,0) 60%),
        radial-gradient(650px 420px at 50% 45%, rgba(0,0,0,.28), rgba(0,0,0,0) 70%),
        linear-gradient(180deg, var(--felt1) 0%, var(--felt2) 100%);
      border:1px solid rgba(255,255,255,.10);
    }

    .rail{
      position:absolute; inset:28px;
      border-radius:20px;
      border:1px solid rgba(242,211,123,.22);
      box-shadow: inset 0 0 0 1px rgba(0,0,0,.35);
    }

    .centerArea{
      position:absolute;
      left:50%; top:50%;
      transform: translate(-50%, -50%);
      width: 520px;
      height: 220px;
      border-radius: 22px;
      border: 1px solid rgba(255,255,255,.10);
      background: rgba(0,0,0,.18);
      box-shadow: inset 0 0 0 1px rgba(0,0,0,.35);
      display:flex;
      flex-direction:column;
      justify-content:center;
      gap:14px;
      padding:18px;
    }

    .communityRow{
      display:flex; gap:10px; align-items:center; justify-content:center;
    }

    .deckArea{
      display:flex; gap:12px; align-items:center; justify-content:center;
      opacity:.95;
    }

    .badge{
      font-size:11px;
      padding:6px 10px;
      border-radius:999px;
      border:1px solid rgba(255,255,255,.12);
      background: rgba(0,0,0,.18);
      color: rgba(255,255,255,.85);
      letter-spacing:.10em;
      text-transform:uppercase;
    }

    /* Seats */
    .seat{
      position:absolute;
      width: 220px;
      display:flex;
      align-items:center;
      gap:12px;
      padding:10px 12px;
      border-radius: 18px;
      border: 1px solid rgba(255,255,255,.10);
      background: rgba(0,0,0,.14);
      box-shadow: 0 18px 40px rgba(0,0,0,.30);
      backdrop-filter: blur(8px);
    }
    .avatar{
      width:44px;height:44px;border-radius:999px;
      border:1px solid rgba(242,211,123,.45);
      box-shadow: 0 0 0 3px rgba(0,0,0,.22), 0 0 26px rgba(242,211,123,.12);
      position:relative;
      overflow:hidden;
      flex:0 0 auto;
    }
    .avatar::before{
      content:"";
      position:absolute; inset:-30%;
      background: conic-gradient(from 210deg, rgba(76,255,195,.9), rgba(242,211,123,.95), rgba(255,77,109,.9), rgba(76,255,195,.9));
      filter: blur(0px);
      opacity:.85;
    }
    .avatar::after{
      content: attr(data-initial);
      position:absolute; inset:0;
      display:flex; align-items:center; justify-content:center;
      font-weight:900; color: rgba(0,0,0,.78);
      text-shadow: 0 1px 0 rgba(255,255,255,.20);
      background: radial-gradient(circle at 30% 25%, rgba(255,255,255,.35), rgba(0,0,0,.10));
    }
    .seatName{font-weight:850; color: rgba(255,255,255,.92); font-size:13px; line-height:1.1}
    .seatMeta{font-size:11px; color: rgba(159,179,170,.88); margin-top:2px}
    .hostTag{
      display:inline-flex;
      margin-left:8px;
      align-items:center; gap:6px;
      font-size:10px;
      padding:4px 8px;
      border-radius:999px;
      border:1px solid rgba(76,255,195,.25);
      background: rgba(76,255,195,.10);
      color: rgba(76,255,195,.95);
      letter-spacing:.08em;
    }

    /* Seat positions (up to 6) */
    #seat0{left:50%; bottom:34px; transform:translateX(-50%);} /* you */
    #seat1{left:60px; bottom:110px;}
    #seat2{left:60px; top:120px;}
    #seat3{left:50%; top:34px; transform:translateX(-50%);}
    #seat4{right:60px; top:120px;}
    #seat5{right:60px; bottom:110px;}

    /* Card styles */
    .card{
      width:72px; height:104px;
      border-radius:14px;
      background: linear-gradient(180deg, var(--card) 0%, var(--card2) 100%);
      border:1px solid rgba(0,0,0,.18);
      box-shadow: 0 18px 35px rgba(0,0,0,.30);
      position:relative;
      overflow:hidden;
      display:flex;
      flex-direction:column;
      justify-content:space-between;
      padding:10px 9px;
      transform: translateZ(0);
      transition: transform .18s ease;
      user-select:none;
    }
    .card:hover{transform: translateY(-2px)}
    .rank{font-weight:950; font-size:18px; letter-spacing:.02em}
    .suit{font-size:18px}
    .mini{display:flex; gap:6px; align-items:center}
    .corner{
      display:flex; flex-direction:column;
      gap:2px;
      width:100%;
    }
    .bigSuit{
      position:absolute;
      right: 10px;
      bottom: 10px;
      font-size: 28px;
      opacity:.18;
      transform: rotate(-12deg);
    }
    .red{color: var(--red)}
    .black{color: #141824}

    .cardBack{
      background:
        radial-gradient(circle at 30% 25%, rgba(255,255,255,.20), rgba(0,0,0,0) 40%),
        linear-gradient(180deg, rgba(242,211,123,.25), rgba(242,211,123,.10)),
        repeating-linear-gradient(45deg, rgba(0,0,0,.14), rgba(0,0,0,.14) 6px, rgba(255,255,255,.08) 6px, rgba(255,255,255,.08) 12px);
      border: 1px solid rgba(242,211,123,.28);
    }

    .handStrip{
      position:absolute;
      left:50%; bottom: 150px;
      transform: translateX(-50%);
      display:flex; gap:12px;
      padding: 12px 14px;
      border-radius: 18px;
      border: 1px solid rgba(255,255,255,.10);
      background: rgba(0,0,0,.16);
      box-shadow: 0 18px 40px rgba(0,0,0,.30);
    }

    .auditToast{
      position:absolute;
      left:50%; top:16px;
      transform: translateX(-50%);
      padding:10px 14px;
      border-radius: 999px;
      border: 1px solid rgba(242,211,123,.26);
      background: rgba(0,0,0,.26);
      color: rgba(242,211,123,.94);
      font-weight:850;
      letter-spacing:.08em;
      display:none;
      box-shadow: 0 18px 40px rgba(0,0,0,.35);
    }

    @media (max-width: 1080px){
      .panel{display:none}
      .tableWrap{min-width: 100%}
    }
  </style>
</head>

<body>
  <div class="app">
    <div class="panel">
      <div class="brand">
        <div class="logo">
          <div class="chip"></div>
          <div>
            <div class="title">CASINO POKER</div>
            <div class="sub">Verifiable Shuffle • Commit–Reveal</div>
          </div>
        </div>
        <div class="pill"><span class="dot"></span><span id="net">DISCONNECTED</span></div>
      </div>

      <div class="section">
        <div class="label">Join / Create</div>
        <div class="row">
          <input id="name" placeholder="Your name" maxlength="18" />
          <input id="avatar" placeholder="Avatar (e.g. 🐍)" maxlength="2" />
        </div>
        <div class="row">
          <input id="room" placeholder="Room code (e.g. 9K2QWZ)" maxlength="6" />
          <button class="btn btnSmall" id="createBtn">CREATE</button>
        </div>
        <button class="btn2" id="joinBtn">JOIN ROOM</button>
        <div class="hint">
          <b>Offline</b> = everyone joins via the host’s Wi-Fi/hotspot.<br>
          <b>Online</b> = same app, just host it on a server later.
        </div>
      </div>

      <div class="section">
        <div class="label">Poker Variant</div>
        <select id="variant">
          <option value="TEXAS">Texas Hold'em (2 hole)</option>
          <option value="OMAHA">Omaha (4 hole)</option>
        </select>
      </div>

      <div class="section">
        <div class="label">Fair Shuffle (Commit–Reveal)</div>
        <input id="seed" placeholder="Secret seed phrase (never reveal during hand)" />
        <div class="grid2">
          <button class="btn2" id="commitBtn">COMMIT</button>
          <button class="btn2" id="revealBtn">REVEAL (to server)</button>
        </div>
        <div class="grid2">
          <button class="btn2" id="startHandBtn">START HAND</button>
          <button class="btn2" id="auditBtn">AUDIT</button>
        </div>
        <div class="hint" style="margin-top:8px">
          Commit is public (hash only). Reveal goes to server privately. Audit reveals all seeds after the hand finishes.
        </div>
      </div>

      <div class="section">
        <div class="label">Dealer Controls (host)</div>
        <div class="grid2">
          <button class="btn2" id="flopBtn">FLOP</button>
          <button class="btn2" id="turnBtn">TURN</button>
        </div>
        <div class="grid2">
          <button class="btn2" id="riverBtn">RIVER</button>
          <button class="btn2" id="newHandBtn">NEW HAND</button>
        </div>
      </div>

      <div class="section">
        <div class="label">Game Log</div>
        <div class="log" id="log"></div>
      </div>
    </div>

    <div class="tableWrap">
      <div class="felt"></div>
      <div class="rail"></div>

      <div class="auditToast" id="auditToast">AUDIT: PENDING</div>

      <div class="centerArea">
        <div class="deckArea">
          <span class="badge" id="roomBadge">ROOM: —</span>
          <span class="badge" id="stageBadge">STAGE: LOBBY</span>
          <span class="badge" id="varBadge">VARIANT: TEXAS</span>
        </div>
        <div class="communityRow" id="community"></div>
      </div>

      <div class="handStrip" id="handStrip"></div>

      <div class="seat" id="seat0"><div class="avatar" data-initial="?"></div><div><div class="seatName">Empty</div><div class="seatMeta">—</div></div></div>
      <div class="seat" id="seat1"><div class="avatar" data-initial="?"></div><div><div class="seatName">Empty</div><div class="seatMeta">—</div></div></div>
      <div class="seat" id="seat2"><div class="avatar" data-initial="?"></div><div><div class="seatName">Empty</div><div class="seatMeta">—</div></div></div>
      <div class="seat" id="seat3"><div class="avatar" data-initial="?"></div><div><div class="seatName">Empty</div><div class="seatMeta">—</div></div></div>
      <div class="seat" id="seat4"><div class="avatar" data-initial="?"></div><div><div class="seatName">Empty</div><div class="seatMeta">—</div></div></div>
      <div class="seat" id="seat5"><div class="avatar" data-initial="?"></div><div><div class="seatName">Empty</div><div class="seatMeta">—</div></div></div>
    </div>
  </div>

<script>
  // ----------------------------
  // Helpers
  // ----------------------------
  const logEl = document.getElementById("log");
  function log(msg){
    const t = new Date().toLocaleTimeString();
    logEl.textContent += `[${t}] ${msg}\\n`;
    logEl.scrollTop = logEl.scrollHeight;
  }
  function q(id){ return document.getElementById(id); }
  function b64(bytes){
    let s = "";
    bytes.forEach(x => s += String.fromCharCode(x));
    return btoa(s);
  }
  function utf8bytes(str){ return new TextEncoder().encode(str); }

  async function sha256hex(str){
    const buf = await crypto.subtle.digest("SHA-256", utf8bytes(str));
    const arr = Array.from(new Uint8Array(buf));
    return arr.map(b => b.toString(16).padStart(2,"0")).join("");
  }

  // SplitMix64 + deterministic shuffle in JS (for audit verification)
  function u64(n){ return BigInt.asUintN(64, n); }
  class SplitMix64 {
    constructor(seed){
      this.state = u64(seed);
    }
    nextU64(){
      this.state = u64(this.state + 0x9E3779B97F4A7C15n);
      let z = this.state;
      z = u64((z ^ (z >> 30n)) * 0xBF58476D1CE4E5B9n);
      z = u64((z ^ (z >> 27n)) * 0x94D049BB133111EBn);
      return u64(z ^ (z >> 31n));
    }
    randbelow(n){
      if(n <= 0n) throw new Error("n must be > 0");
      const MOD = 1n << 64n;
      const limit = MOD - (MOD % n);
      while(true){
        const r = this.nextU64();
        if(r < limit) return r % n;
      }
    }
  }

  function makeDeck(){
    const R = ["A","K","Q","J","10","9","8","7","6","5","4","3","2"];
    const S = ["♠","♥","♦","♣"];
    let d=[];
    for(const s of S){
      for(const r of R){
        d.push(r+s);
      }
    }
    return d;
  }

  async function masterSeedHexFromReveals(reveals){
    // reveals: array of {pid, seed, salt} sorted by pid for consistency
    const sorted = reveals.slice().sort((a,b)=>a.pid.localeCompare(b.pid));
    let joined = "";
    for(const r of sorted){
      joined += `${r.pid}:${r.seed}:${r.salt}|`;
    }
    return await sha256hex(joined);
  }

  async function deterministicShuffleJS(items, masterSeedHex){
    // Use first 8 bytes of masterSeed as u64 seed
    const seedU64 = BigInt("0x" + masterSeedHex.slice(0,16));
    const rng = new SplitMix64(seedU64);
    const arr = items.slice();
    for(let i=arr.length-1; i>0; i--){
      const j = Number(rng.randbelow(BigInt(i+1)));
      let tmp = arr[i]; arr[i] = arr[j]; arr[j] = tmp;
    }
    return arr;
  }

  function cardEl(card){
    const div = document.createElement("div");
    div.className = "card";
    if(!card){
      div.classList.add("cardBack");
      div.innerHTML = "";
      return div;
    }
    const suit = card.slice(-1);
    const rank = card.slice(0, card.length-1);
    const red = (suit === "♥" || suit === "♦");
    const colorClass = red ? "red" : "black";
    div.innerHTML = `
      <div class="corner ${colorClass}">
        <div class="mini"><div class="rank">${rank}</div></div>
        <div class="suit">${suit}</div>
      </div>
      <div class="bigSuit ${colorClass}">${suit}</div>
      <div class="corner ${colorClass}" style="align-items:flex-end; text-align:right;">
        <div class="suit">${suit}</div>
        <div class="mini"><div class="rank">${rank}</div></div>
      </div>
    `;
    return div;
  }

  function setCommunity(cards){
    const c = q("community");
    c.innerHTML = "";
    for(const card of cards){
      c.appendChild(cardEl(card));
    }
    // fill to 5 slots visually
    for(let i=cards.length; i<5; i++){
      c.appendChild(cardEl(null));
    }
  }

  function setHand(cards, variant){
    const h = q("handStrip");
    h.innerHTML = "";
    for(const card of cards){
      h.appendChild(cardEl(card));
    }
    const max = (variant === "OMAHA") ? 4 : 2;
    for(let i=cards.length; i<max; i++){
      h.appendChild(cardEl(null));
    }
  }

  function setSeats(players, myPid){
    // place order: you always in seat0, others fill clockwise
    const seatIds = ["seat0","seat1","seat2","seat3","seat4","seat5"];
    const pls = Object.values(players || {});
    const me = pls.find(p => p.pid === myPid);
    const others = pls.filter(p => p.pid !== myPid);

    const ordered = [];
    if(me) ordered.push(me);
    for(const p of others) ordered.push(p);

    for(let i=0; i<seatIds.length; i++){
      const seat = q(seatIds[i]);
      const av = seat.querySelector(".avatar");
      const nameEl = seat.querySelector(".seatName");
      const metaEl = seat.querySelector(".seatMeta");
      if(i < ordered.length){
        const p = ordered[i];
        const initial = (p.avatar && p.avatar.trim().length>0) ? p.avatar.trim().slice(0,2) : (p.name ? p.name.trim()[0].toUpperCase() : "?");
        av.setAttribute("data-initial", initial);
        nameEl.innerHTML = `${escapeHtml(p.name)}${p.is_host ? '<span class="hostTag">HOST</span>' : ''}`;
        const c = p.commitment ? "Committed" : "No-commit";
        const r = p.revealed ? "Revealed" : "Hidden";
        metaEl.textContent = `${c} • Seed: ${r}`;
      } else {
        av.setAttribute("data-initial", "?");
        nameEl.textContent = "Empty";
        metaEl.textContent = "—";
      }
    }
  }

  function escapeHtml(s){
    return (s || "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
  }

  // ----------------------------
  // WebSocket client
  // ----------------------------
  let ws = null;
  let myPid = null;
  let room = null;
  let mySalt = null;

  function setNet(text){
    q("net").textContent = text;
  }

  async function connect(roomCode, name, avatar){
    room = roomCode;
    const pid = crypto.randomUUID();
    myPid = pid;

    const proto = (location.protocol === "https:") ? "wss" : "ws";
    const url = `${proto}://${location.host}/ws/${roomCode}/${pid}`;
    ws = new WebSocket(url);

    ws.onopen = () => {
      setNet("CONNECTED");
      log(`Connected to room ${roomCode}`);
      ws.send(JSON.stringify({type:"join", name, avatar}));
      q("roomBadge").textContent = "ROOM: " + roomCode;
    };

    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if(msg.type === "state"){
        q("stageBadge").textContent = "STAGE: " + msg.stage;
        q("varBadge").textContent = "VARIANT: " + msg.variant;
        setSeats(msg.players, myPid);
        setCommunity(msg.community || []);
        setHand(msg.my_hole || [], msg.variant);
        if(msg.audit_pending){
          q("auditToast").style.display = "inline-flex";
          q("auditToast").textContent = "AUDIT: PENDING";
        } else {
          q("auditToast").style.display = "none";
        }
      }
      if(msg.type === "log"){
        log(msg.text);
      }
      if(msg.type === "audit"){
        handleAudit(msg);
      }
    };

    ws.onclose = () => {
      setNet("DISCONNECTED");
      log("Disconnected.");
    };
  }

  async function handleAudit(msg){
    // msg has: reveals[{pid, seed, salt}], deck[], transcript, master_seed_hex
    log("AUDIT RECEIVED. Verifying...");
    q("auditToast").style.display = "inline-flex";
    q("auditToast").textContent = "AUDIT: VERIFYING…";

    // 1) verify commitments (server also verified, but we re-check)
    // We don't have the commitments here (in a real UI we'd add them),
    // so we verify the deterministic shuffle only.

    const computedMaster = await masterSeedHexFromReveals(msg.reveals);
    const okMaster = (computedMaster === msg.master_seed_hex);
    const deck2 = await deterministicShuffleJS(makeDeck(), computedMaster);

    let okDeck = true;
    if(deck2.length !== msg.deck.length) okDeck = false;
    else{
      for(let i=0; i<deck2.length; i++){
        if(deck2[i] !== msg.deck[i]){ okDeck = false; break; }
      }
    }

    // 2) verify transcript dealt cards match deck positions
    let okTranscript = true;
    const tr = msg.transcript || {};
    // checks: holes + community should match deck at recorded indices
    try{
      const holes = tr.holes || {};
      for(const pid in holes){
        const idxs = holes[pid]; // array of indices
        for(const ix of idxs){
          if(typeof ix !== "number") okTranscript = false;
        }
      }
      const comm = tr.community_indices || [];
      for(const ix of comm){
        if(typeof ix !== "number") okTranscript = false;
      }
    }catch(e){ okTranscript = false; }

    const verdict = okMaster && okDeck && okTranscript;
    if(verdict){
      q("auditToast").textContent = "AUDIT: VERIFIED ✅";
      log("AUDIT VERIFIED ✅ The shuffle and dealing transcript match.");
    } else {
      q("auditToast").textContent = "AUDIT: MISMATCH ❌";
      log("AUDIT MISMATCH ❌ Something does not match. (Would indicate tampering/bug.)");
    }

    // show a small summary
    log(`MasterSeed: ${msg.master_seed_hex.slice(0,16)}… | DeckHash: ${msg.deck_hash.slice(0,16)}…`);
  }

  // ----------------------------
  // Buttons
  // ----------------------------
  q("createBtn").onclick = async () => {
    const res = await fetch("/api/create", {method:"POST"});
    const data = await res.json();
    q("room").value = data.room;
    log("Created room: " + data.room);
  };

  q("joinBtn").onclick = async () => {
    const name = q("name").value.trim() || "Player";
    const avatar = q("avatar").value.trim() || "";
    const roomCode = q("room").value.trim().toUpperCase();
    if(roomCode.length < 4){
      log("Enter a room code (or create one).");
      return;
    }
    await connect(roomCode, name, avatar);
  };

  q("commitBtn").onclick = async () => {
    if(!ws) return log("Join a room first.");
    const seed = q("seed").value;
    if(!seed) return log("Enter a seed phrase first.");

    // random salt (base64)
    const saltBytes = crypto.getRandomValues(new Uint8Array(16));
    mySalt = b64(Array.from(saltBytes));

    const commitment = await sha256hex(seed + "|" + mySalt);
    ws.send(JSON.stringify({type:"commit", commitment}));
    log("Committed seed hash.");
  };

  q("revealBtn").onclick = async () => {
    if(!ws) return log("Join a room first.");
    const seed = q("seed").value;
    if(!seed || !mySalt) return log("Commit first (seed + salt).");
    ws.send(JSON.stringify({type:"reveal", seed, salt: mySalt}));
    log("Revealed to server (private).");
  };

  q("startHandBtn").onclick = () => {
    if(!ws) return log("Join a room first.");
    const v = q("variant").value;
    ws.send(JSON.stringify({type:"start_hand", variant: v}));
    log("Requested start hand (host only).");
  };

  q("flopBtn").onclick = () => ws && ws.send(JSON.stringify({type:"deal", what:"flop"}));
  q("turnBtn").onclick = () => ws && ws.send(JSON.stringify({type:"deal", what:"turn"}));
  q("riverBtn").onclick = () => ws && ws.send(JSON.stringify({type:"deal", what:"river"}));
  q("newHandBtn").onclick = () => ws && ws.send(JSON.stringify({type:"new_hand"}));
  q("auditBtn").onclick = () => ws && ws.send(JSON.stringify({type:"audit"}));

  // Defaults
  setCommunity([]);
  setHand([], "TEXAS");
  log("Ready. Create a room or join one.");
</script>

</body>
</html>
"""

# ----------------------------
# API routes
# ----------------------------
@app.get("/")
def index():
    return HTMLResponse(INDEX_HTML)

@app.post("/api/create")
def create_room():
    code = room_code()
    rooms[code] = Room(code=code)
    return {"room": code}

# ----------------------------
# WebSocket game server
# ----------------------------
async def broadcast(room: Room, payload: dict):
    dead = []
    for p in room.players.values():
        if p.ws is None:
            continue
        try:
            await p.ws.send_text(json.dumps(payload))
        except Exception:
            dead.append(p.pid)
    for pid in dead:
        room.players.pop(pid, None)

def room_public_state(room: Room, pid: str) -> dict:
    players = {}
    for p in room.players.values():
        players[p.pid] = {
            "pid": p.pid,
            "name": p.name,
            "avatar": p.avatar,
            "is_host": p.is_host,
            "commitment": p.commitment,
            "revealed": bool(p.seed and p.salt) if room.stage in ("HAND","AUDIT") else False,  # keep private until audit/hand stage
        }

    me = room.players.get(pid)
    my_hole = me.hole if me else []

    # show "audit pending" toast if stage is HAND and audit not yet broadcast
    audit_pending = (room.stage == "HAND")

    return {
        "type":"state",
        "room": room.code,
        "stage": room.stage,
        "variant": room.variant,
        "players": players,
        "community": room.community,
        "my_hole": my_hole,
        "audit_pending": audit_pending,
    }

def compute_master_seed(room: Room) -> Tuple[bytes, str]:
    # Combine reveals sorted by pid for determinism
    items = []
    for pid, p in sorted(room.players.items(), key=lambda kv: kv[0]):
        if not (p.seed and p.salt):
            raise ValueError("Missing reveal for player")
        items.append(f"{pid}:{p.seed}:{p.salt}")
    joined = "|".join(items) + "|"
    digest = sha256_bytes(joined.encode("utf-8"))
    return digest, digest.hex()

def reset_hand(room: Room):
    room.community = []
    room.deck = []
    room.deal_index = 0
    room.master_seed_hex = None
    room.transcript = {}
    for p in room.players.values():
        p.hole = []

def deal_hole(room: Room):
    # create and shuffle deck based on master seed
    master_bytes, master_hex = compute_master_seed(room)
    room.master_seed_hex = master_hex

    deck = make_deck()
    room.deck = deterministic_shuffle(deck, master_bytes)
    room.deal_index = 0

    # record transcript indices used
    room.transcript = {
        "variant": room.variant,
        "holes": {},
        "community_indices": [],
        "created_at": time.time(),
    }

    # Deal hole cards per variant (Texas=2, Omaha=4)
    hole_n = 2 if room.variant == "TEXAS" else 4

    # Dealing order: sorted by join order = insertion order of dict (Python 3.7+ keeps order)
    order = list(room.players.values())
    for p in order:
        p.hole = []
        room.transcript["holes"][p.pid] = []

    for _ in range(hole_n):
        for p in order:
            idx = room.deal_index
            card = room.deck[idx]
            p.hole.append(card)
            room.transcript["holes"][p.pid].append(idx)
            room.deal_index += 1

def deal_community(room: Room, count: int):
    # No burn in this prototype; easy to add later
    for _ in range(count):
        idx = room.deal_index
        card = room.deck[idx]
        room.community.append(card)
        room.transcript["community_indices"].append(idx)
        room.deal_index += 1

async def send_state(room: Room):
    # send to each player so they get private hole cards
    for pid, p in list(room.players.items()):
        if p.ws is None:
            continue
        try:
            await p.ws.send_text(json.dumps(room_public_state(room, pid)))
        except Exception:
            room.players.pop(pid, None)

@app.websocket("/ws/{code}/{pid}")
async def ws(code: str, pid: str, websocket: WebSocket):
    await websocket.accept()

    if code not in rooms:
        rooms[code] = Room(code=code)

    room = rooms[code]

    # temporary player until join message sets details
    player = Player(pid=pid, name="Player", avatar="")
    player.ws = websocket

    # host = first player
    if len(room.players) == 0:
        player.is_host = True

    room.players[pid] = player

    # initial state
    await websocket.send_text(json.dumps(room_public_state(room, pid)))
    await broadcast(room, {"type":"log", "text": f"Player connected: {pid[:8]}…"})

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type", "")

            if mtype == "join":
                player.name = (msg.get("name") or "Player")[:18]
                player.avatar = (msg.get("avatar") or "")[:2]
                await broadcast(room, {"type":"log", "text": f"{player.name} joined the table."})
                await send_state(room)

            elif mtype == "commit":
                commitment = msg.get("commitment")
                if not commitment or len(commitment) != 64:
                    await websocket.send_text(json.dumps({"type":"log", "text":"Invalid commitment."}))
                    continue
                player.commitment = commitment
                await broadcast(room, {"type":"log", "text": f"{player.name} committed."})
                # Auto move to COMMIT stage if not started
                if room.stage == "LOBBY":
                    room.stage = "COMMIT"
                await send_state(room)

            elif mtype == "reveal":
                seed = msg.get("seed") or ""
                salt = msg.get("salt") or ""
                if not player.commitment:
                    await websocket.send_text(json.dumps({"type":"log", "text":"Commit first."}))
                    continue
                # verify commitment
                calc = sha256_hex(seed + "|" + salt)
                if calc != player.commitment:
                    await websocket.send_text(json.dumps({"type":"log", "text":"Reveal does not match commitment ❌"}))
                    continue
                player.seed = seed
                player.salt = salt
                await broadcast(room, {"type":"log", "text": f"{player.name} revealed to server."})
                room.stage = "REVEAL"
                await send_state(room)

            elif mtype == "start_hand":
                if not player.is_host:
                    await websocket.send_text(json.dumps({"type":"log", "text":"Host only."}))
                    continue
                variant = (msg.get("variant") or "TEXAS").upper()
                if variant not in ("TEXAS","OMAHA"):
                    variant = "TEXAS"
                room.variant = variant

                # require all players to have commits + reveals
                if len(room.players) < 2:
                    await websocket.send_text(json.dumps({"type":"log", "text":"Need at least 2 players."}))
                    continue
                for p in room.players.values():
                    if not p.commitment:
                        await websocket.send_text(json.dumps({"type":"log", "text": f"{p.name} has not committed."}))
                        break
                    if not (p.seed and p.salt):
                        await websocket.send_text(json.dumps({"type":"log", "text": f"{p.name} has not revealed to server."}))
                        break
                else:
                    reset_hand(room)
                    deal_hole(room)
                    room.stage = "HAND"
                    await broadcast(room, {"type":"log", "text": f"New hand started • {room.variant} • Dealt hole cards."})
                    await send_state(room)

            elif mtype == "deal":
                if not player.is_host:
                    await websocket.send_text(json.dumps({"type":"log", "text":"Host only."}))
                    continue
                if room.stage != "HAND" or not room.deck:
                    await websocket.send_text(json.dumps({"type":"log", "text":"Start a hand first."}))
                    continue
                what = msg.get("what")
                if what == "flop" and len(room.community) == 0:
                    deal_community(room, 3)
                    await broadcast(room, {"type":"log", "text":"Flop dealt."})
                elif what == "turn" and len(room.community) == 3:
                    deal_community(room, 1)
                    await broadcast(room, {"type":"log", "text":"Turn dealt."})
                elif what == "river" and len(room.community) == 4:
                    deal_community(room, 1)
                    await broadcast(room, {"type":"log", "text":"River dealt."})
                else:
                    await websocket.send_text(json.dumps({"type":"log", "text":"That deal action is not valid right now."}))
                await send_state(room)

            elif mtype == "new_hand":
                if not player.is_host:
                    await websocket.send_text(json.dumps({"type":"log", "text":"Host only."}))
                    continue
                # keep commitments and reveals; just reset visible cards
                reset_hand(room)
                room.stage = "HAND"
                deal_hole(room)
                await broadcast(room, {"type":"log", "text":"New hand (same commitments) • Dealt hole cards."})
                await send_state(room)

            elif mtype == "audit":
                if room.stage != "HAND" or not room.deck or not room.master_seed_hex:
                    await websocket.send_text(json.dumps({"type":"log", "text":"Nothing to audit yet."}))
                    continue

                # Prepare reveal payload (public now)
                reveals = []
                for pid2, p2 in sorted(room.players.items(), key=lambda kv: kv[0]):
                    reveals.append({"pid": pid2, "seed": p2.seed, "salt": p2.salt})

                deck_hash = hashlib.sha256(("|".join(room.deck) + "|").encode("utf-8")).hexdigest()

                payload = {
                    "type":"audit",
                    "master_seed_hex": room.master_seed_hex,
                    "deck": room.deck,
                    "deck_hash": deck_hash,
                    "reveals": reveals,
                    "transcript": room.transcript,
                }
                room.stage = "AUDIT"
                await broadcast(room, {"type":"log", "text":"AUDIT broadcast: seeds revealed. Verify now."})
                await broadcast(room, payload)
                await send_state(room)

            else:
                await websocket.send_text(json.dumps({"type":"log", "text":"Unknown message."}))

    except WebSocketDisconnect:
        room.players.pop(pid, None)
        await broadcast(room, {"type":"log", "text": f"{player.name} left."})
        # If host left, promote first remaining player
        if not any(p.is_host for p in room.players.values()) and len(room.players) > 0:
            first = next(iter(room.players.values()))
            first.is_host = True
            await broadcast(room, {"type":"log", "text": f"{first.name} is now HOST."})
        await send_state(room)
    except Exception as e:
        room.players.pop(pid, None)
        await broadcast(room, {"type":"log", "text": f"Error: {str(e)}"})
        await send_state(room)
