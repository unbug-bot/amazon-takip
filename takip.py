#!/usr/bin/env python3
"""
GitHub Actions'ta çalışan yavaş Amazon.com.tr takibi.

Her çalıştırma küçük bir sayfa bütçesi (varsayılan 8) kullanır ve işleri sırayla yapar:
  kontrol : takipteki beden varyantlarının fiyat/stok kontrolü (en eskisi önce)
  arama   : arama sayfaları (Dior fiyatları buradan güncellenir; pantolon/gömlek için aday toplar)
  kesif   : aday ürün sayfasını açıp aranan bedeni arar
Ayrıştırma ve kayıt mantığı tracker.py'den gelir. Sonunda site/index.html üretilir.
"""
from __future__ import annotations

import html
import os
import random
import re
import time
from datetime import datetime, timedelta

import tracker as T
from tracker import log, tl

OTURUM = T.ROOT / "oturum.json"
SITE = T.ROOT / "site"

ONLINE_VARSAYILAN = {
    "sayfa_butcesi": 8,
    "bekleme_saniye": [20, 40],
    "kontrol_saat": 4,
    "arama_saat_bedensiz": 6,
    "arama_saat_bedenli": 24,
}

EK_SCHEMA = """
CREATE TABLE IF NOT EXISTS durum(anahtar TEXT PRIMARY KEY, deger TEXT);
CREATE TABLE IF NOT EXISTS adaylar(asin TEXT PRIMARY KEY, takip_id TEXT, baslik TEXT, eklenme TEXT);
CREATE TABLE IF NOT EXISTS calismalar(ts TEXT, sayfa INTEGER, captcha INTEGER, sonuc TEXT);
CREATE TABLE IF NOT EXISTS saticilar(id TEXT PRIMARY KEY, ad TEXT, oran REAL, kapsam TEXT, ts TEXT);
"""


def sema_guncelle(con) -> None:
    kolonlar = {r[1] for r in con.execute("PRAGMA table_info(urunler)")}
    if "satici" not in kolonlar:
        con.execute("ALTER TABLE urunler ADD COLUMN satici TEXT")
    if "satici_uygun" not in kolonlar:
        con.execute("ALTER TABLE urunler ADD COLUMN satici_uygun INTEGER DEFAULT 1")
    con.commit()


def simdi_s() -> str:
    return datetime.now().isoformat(timespec="seconds")


def dget(con, k):
    r = con.execute("SELECT deger FROM durum WHERE anahtar=?", (k,)).fetchone()
    return r[0] if r else None


def dset(con, k, v) -> None:
    con.execute("INSERT OR REPLACE INTO durum VALUES(?,?)", (k, str(v)))
    con.commit()


def eski_mi(ts, saat) -> bool:
    return ts is None or datetime.fromisoformat(ts) < datetime.now() - timedelta(hours=saat)


# ---------------------------------------------------------------- tarayıcı

class OnlineTarayici:
    """tracker.Tarayici ile aynı arayüz (git, page). Oturum çerezleri dosyada saklanır."""

    def __init__(self, o: dict):
        from playwright.sync_api import sync_playwright
        self.o = o
        self.butce = int(o["sayfa_butcesi"])
        self.istek = 0
        self.captcha = 0
        self.pw = sync_playwright().start()
        self.tarayici = self.pw.chromium.launch(headless=True)
        kw = dict(locale="tr-TR", timezone_id="Europe/Istanbul",
                  viewport={"width": 1366, "height": 900})
        if OTURUM.exists():
            kw["storage_state"] = str(OTURUM)
        self.ctx = self.tarayici.new_context(**kw)
        self.page = self.ctx.new_page()

    def kalan(self) -> int:
        return self.butce - self.istek

    def _yukle(self, url: str) -> None:
        if self.istek >= self.butce + 6:  # keşif birkaç sayfa taşabilir, fazlası değil
            raise T.LimitAsildi()
        if self.istek:
            time.sleep(random.uniform(*self.o["bekleme_saniye"]))
        self.istek += 1
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)

    def _captcha_mi(self) -> bool:
        return bool(self.page.query_selector('form[action*="validateCaptcha"]')) \
            or "captcha" in self.page.url.lower()

    def git(self, url: str) -> None:
        self._yukle(url)
        if self._captcha_mi():
            self.captcha += 1
            log("  CAPTCHA; ana sayfa açılıp bir kez yeniden denenecek")
            self._yukle(T.BASE)
            self._yukle(url)
            if self._captcha_mi():
                self.captcha += 1
                raise T.DogrulamaGerekli(url)
        try:
            btn = self.page.query_selector("#sp-cc-accept")
            if btn:
                btn.click(timeout=3000)
        except Exception:
            pass

    def kapat(self) -> None:
        for f in (lambda: self.ctx.storage_state(path=str(OTURUM)),
                  self.ctx.close, self.tarayici.close, self.pw.stop):
            try:
                f()
            except Exception:
                pass


# ---------------------------------------------------------------- saat doğrulama

SAAT_JS = r"""() => {
  const q = s => document.querySelector(s);
  const t = s => { const e = q(s); return e ? e.textContent.replace(/\s+/g, ' ').trim() : ''; };
  const temiz = x => x.replace(/[\u200e\u200f]/g, '').replace(/\s+/g, ' ').trim();
  const alanlar = [];
  document.querySelectorAll('#productOverview_feature_div tr, #poExpander tr').forEach(tr => {
    const td = tr.querySelectorAll('td');
    if (td.length >= 2) alanlar.push([temiz(td[0].textContent), temiz(td[1].textContent)]);
  });
  document.querySelectorAll('#productDetails_techSpec_section_1 tr, #productDetails_detailBullets_sections1 tr, table.prodDetTable tr')
    .forEach(tr => { const th = tr.querySelector('th'), td = tr.querySelector('td');
      if (th && td) alanlar.push([temiz(th.textContent), temiz(td.textContent)]); });
  document.querySelectorAll('#detailBullets_feature_div li').forEach(li => {
    const p = temiz(li.textContent).split(':');
    if (p.length >= 2) alanlar.push([p[0].trim(), p.slice(1).join(':').trim()]);
  });
  const sp = q('#sellerProfileTriggerId');
  const href = sp ? (sp.getAttribute('href') || '') : '';
  return {
    alanlar,
    maddeler: [...document.querySelectorAll('#feature-bullets li')].map(li => temiz(li.textContent)),
    aciklama: t('#productDescription').slice(0, 3000),
    yol: t('#wayfinding-breadcrumbs_feature_div'),
    satici_ad: sp ? temiz(sp.textContent) : '',
    satici_id: (href.match(/seller=([A-Z0-9]+)/) || [])[1] || '',
    satici_metin: t('#merchantInfoFeature_feature_div') || t('#merchant-info') || t('#tabular-buybox'),
  };
}"""

SATICI_JS = r"""() => {
  let tablo = null;
  const tb = document.querySelector('#feedback-summary-table');
  if (tb) for (const tr of tb.querySelectorAll('tr')) {
    const c = [...tr.querySelectorAll('td')].map(x => x.textContent.replace(/\s+/g, ' ').trim());
    if (c.length > 1 && /olumlu|positive/i.test(c[0])) tablo = c;
  }
  return {tablo, metin: document.body ? document.body.innerText.slice(0, 15000) : ''};
}"""

OTOMATIK = ("otomatik", "automatic", "automatik", "self winding", "kendinden kurmalı", "kendi kendini kuran")
PILLI = ("kuvars", "quartz", "eco drive", "ecodrive", "solar", "güneş enerjili", "kinetic")
SAFIR = ("safir", "sapphire")
KAPLAMA_DESEN = r"(safir|sapphire)\s*(kaplama|kaplamalı|coated|coating)|(kaplama|kaplamalı|coated)\s*(safir|sapphire)"
SAFIR_DESEN = r"(safir|sapphire)\s*(cam|camı|camlı|kristal|crystal|glass)|(cam|camı|kristal|crystal|glass)\s*(safir|sapphire)"
BASKA_CAM = ("hardlex", "mineral", "akrilik", "acrylic", "pleksi")


def _alan(d, *anahtar) -> str:
    return " ".join(v for k, v in d["alanlar"] if any(a in T.norm(k) for a in anahtar))


def _var(sade: str, kelimeler) -> bool:
    return any(T._kelime_var(sade, k) for k in kelimeler)


def saat_dogrula(b: dict, d: dict, k: dict):
    """(uygun_mu, elenme_nedeni, marka). Emin olunamayan her durumda elenir."""
    S = T._sade
    # ürün gerçekten kol saati mi
    yol = S(d.get("yol", ""))
    if yol:
        if "saat" not in yol or _var(yol, ("kayış", "kayışlar", "aksesuar", "aksesuarlar")):
            return False, "kategori saat değil", ""
    elif "saat" not in S(b["baslik"]) and "watch" not in S(b["baslik"]):
        return False, "başlıkta saat yok", ""
    # marka: önce teknik tablodaki Marka alanı, yoksa marka satırı
    marka_kaynak = S(_alan(d, "marka", "brand") or b.get("marka", ""))
    marka = next((m for m in k["markalar"] if T._kelime_var(marka_kaynak, m)), "")
    if not marka:
        return False, f"marka uygun değil ({marka_kaynak[:30] or 'okunamadı'})", ""
    # mekanizma
    baslik_madde = S(b["baslik"] + " " + " ".join(d["maddeler"]))
    tum = S(b["baslik"] + " " + " ".join(v for _, v in d["alanlar"]) + " " + " ".join(d["maddeler"]))
    if _var(tum, PILLI):
        return False, "kuvars/pilli/solar ifadesi var", marka
    hareket = S(_alan(d, "hareket", "movement", "mekanizma"))
    if not (_var(hareket, OTOMATIK) or _var(baslik_madde, OTOMATIK)):
        return False, "otomatik olduğu yazmıyor", marka
    # cam
    cam = S(_alan(d, "cam", "pencere", "crystal", "window", "kristal"))
    if cam and (_var(cam, SAFIR) or _var(cam, BASKA_CAM)):
        if re.search(KAPLAMA_DESEN, cam) or not _var(cam, SAFIR) or _var(cam, BASKA_CAM):
            return False, f"cam: {cam[:30]}", marka
    else:
        metin = S(b["baslik"] + " " + " ".join(d["maddeler"]) + " " + d.get("aciklama", ""))
        if not re.search(SAFIR_DESEN, metin):
            return False, "safir cam yazmıyor", marka
        if re.search(KAPLAMA_DESEN, metin) or _var(metin, ("hardlex",)) or re.search(
                r"mineral\s*(cam|kristal|crystal|glass)", metin):
            return False, "cam bilgisi çelişkili", marka
    return True, "", marka


def satici_puani(tr, sid: str):
    """Satıcı sayfasından olumlu yüzde. (oran, 'toplam' | '12ay') ya da (None, '')."""
    tr.git(f"{T.BASE}/sp?seller={sid}")
    s = tr.page.evaluate(SATICI_JS)
    if s["tablo"]:
        m = re.search(r"(\d{1,3})", s["tablo"][-1])  # son sütun: tüm zamanlar
        if m:
            return float(m.group(1)), "toplam"
    metin = s["metin"]
    for desen in (r"(?:tüm zamanlar|ömür boyu|lifetime)[^%\n]{0,60}%\s*(\d{1,3})",
                  r"%\s*(\d{1,3})[^%\n]{0,60}(?:tüm zamanlar|ömür boyu|lifetime)"):
        m = re.search(desen, metin, re.I)
        if m:
            return float(m.group(1)), "toplam"
    m = re.search(r"%\s*(\d{1,3})\s*olumlu", metin, re.I) or \
        re.search(r"(\d{1,3})\s*%\s*(?:olumlu|positive)", metin, re.I)
    if m:
        return float(m.group(1)), "12ay"
    return None, ""


def satici_degerlendir(tr, con, d: dict, k: dict):
    """(uygun_mu, elenme_nedeni, etiket)."""
    ad = d.get("satici_ad", "").strip()
    if not ad and re.search(r"(satıcı|sold by)\s*:?\s*amazon|amazon\S*\s*tarafından satıl",
                            T.norm(d.get("satici_metin", ""))):
        ad = "Amazon.com.tr"
    if not ad:
        return False, "satıcı okunamadı", ""
    if any(T._sade(g) == T._sade(ad) for g in k.get("guvenilir_saticilar", [])):
        return True, "", ad
    sid = d.get("satici_id", "")
    if not sid:
        return False, f"satıcı kimliği okunamadı ({ad})", ad
    r = con.execute("SELECT * FROM saticilar WHERE id=?", (sid,)).fetchone()
    if r and not eski_mi(r["ts"], 24 * 30):
        oran, kapsam = r["oran"], r["kapsam"]
    else:
        oran, kapsam = satici_puani(tr, sid)
        con.execute("INSERT OR REPLACE INTO saticilar VALUES(?,?,?,?,?)", (sid, ad, oran, kapsam, simdi_s()))
        con.commit()
    if oran is None:
        return False, f"satıcı puanı okunamadı ({ad})", ad
    etiket = f"{ad} %{oran:.0f}" + (" (12 ay)" if kapsam == "12ay" else "")
    if oran < float(k["satici_min_olumlu"]):
        return False, f"satıcı puanı düşük ({etiket})", etiket
    return True, "", etiket


def _kesfet_saat(tr, con, t, a, olaylar, asin) -> None:
    k = t["saat"]
    b = T.urun_sayfasi(tr, asin)
    if not b:
        T.taranma_kaydi(con, asin, t["id"], False)
        return
    d = tr.page.evaluate(SAAT_JS)
    ok, neden, marka = saat_dogrula(b, d, k)
    if ok and (b["fiyat"] is None or b["fiyat"] > float(k["fiyat_ust"])):
        ok, neden = False, f"fiyat {tl(b['fiyat'])}"
    etiket = ""
    if ok:
        ok, neden, etiket = satici_degerlendir(tr, con, d, k)
    T.taranma_kaydi(con, asin, t["id"], ok)
    log(f"     saat: {'UYGUN' if ok else 'elendi, ' + neden} | {marka or '?'} | {tl(b['fiyat'])}")
    if ok:
        T.kaydet(con, t, a, olaylar, asin=asin, parent=None, baslik=b["baslik"],
                 beden=f"{marka}, satıcı {etiket}", fiyat=b["fiyat"], liste=b["liste"], stokta=b["stokta"])
        con.execute("UPDATE urunler SET satici=?, satici_uygun=1 WHERE asin=?", (etiket, asin))
        con.commit()


# ---------------------------------------------------------------- görevler

def takip_bul(cfg, tid):
    return next((t for t in cfg["takipler"] if t["id"] == tid and t.get("aktif", True)), None)


def gorev_kontrol(tr, con, cfg, a, o, olaylar) -> bool:
    ids = [t["id"] for t in cfg["takipler"] if (t.get("beden") or t.get("saat")) and t.get("aktif", True)]
    if not ids:
        return False
    sinir = (datetime.now() - timedelta(hours=o["kontrol_saat"])).isoformat(timespec="seconds")
    r = con.execute(
        f"SELECT asin,takip_id,parent_asin,beden FROM urunler WHERE aktif=1 AND takip_id IN "
        f"({','.join('?' * len(ids))}) AND (son_kontrol IS NULL OR son_kontrol < ?) "
        f"ORDER BY son_kontrol LIMIT 1", (*ids, sinir)).fetchone()
    if not r:
        return False
    takip = takip_bul(cfg, r["takip_id"])
    b = T.guvenli(T.urun_sayfasi, tr, r["asin"])
    if b:
        dset(con, f"hata:{r['asin']}", 0)
        beden = r["beden"]
        satici = None
        if takip.get("saat"):
            # Satış kutusundaki satıcı değişebilir; her kontrolde yeniden değerlendirilir
            d = tr.page.evaluate(SAAT_JS)
            s_ok, _, etiket = satici_degerlendir(tr, con, d, takip["saat"])
            beden = f"{(r['beden'] or '').split(',')[0]}, satıcı {etiket or '?'}"
            satici = (etiket, int(s_ok))
        T.kaydet(con, takip, a, olaylar, asin=r["asin"], parent=r["parent_asin"], baslik=b["baslik"],
                 beden=beden, fiyat=b["fiyat"], liste=b["liste"], stokta=b["stokta"])
        if satici:
            con.execute("UPDATE urunler SET satici=?, satici_uygun=? WHERE asin=?", (*satici, r["asin"]))
            con.commit()
        log(f"  kontrol: {b['baslik'][:45]} [{r['beden']}] {tl(b['fiyat'])} "
            f"{'stokta' if b['stokta'] else 'stok yok'}")
    else:
        hata = int(dget(con, f"hata:{r['asin']}") or 0) + 1
        dset(con, f"hata:{r['asin']}", hata)
        con.execute("UPDATE urunler SET son_kontrol=? WHERE asin=?", (simdi_s(), r["asin"]))
        if hata >= 5:
            con.execute("UPDATE urunler SET aktif=0 WHERE asin=?", (r["asin"],))
            log(f"  {r['asin']} 5 kez okunamadı, takipten çıkarıldı")
        con.commit()
    return True


def gorev_arama(tr, con, cfg, a, o, olaylar) -> bool:
    for t in cfg["takipler"]:
        if not t.get("aktif", True):
            continue
        saat = o["arama_saat_bedenli"] if (t.get("beden") or t.get("saat")) else o["arama_saat_bedensiz"]
        n = int(t.get("sayfa_sayisi", 3))
        for sorgu in t["aramalar"]:
            for s in range(1, n + 1):
                k = f"arama:{t['id']}:{sorgu}:{s}"
                if eski_mi(dget(con, k), saat):
                    _arama_yap(tr, con, t, a, olaylar, sorgu, s, n, k, saat)
                    return True
    return False


def _arama_yap(tr, con, t, a, olaylar, sorgu, s, n, k, saat) -> None:
    marka = bool(t.get("markalar")) and dget(con, f"markasiz:{t['id']}") is None
    sonuc = T._ara(tr, t, sorgu, s, marka)
    if not sonuc and s == 1 and marka:
        log("  marka filtresiyle sonuç yok; bu takipte filtresiz aranacak")
        dset(con, f"markasiz:{t['id']}", 1)
        sonuc = T._ara(tr, t, sorgu, s, False)
    dset(con, k, simdi_s())
    if not sonuc:
        try:
            sayfa_basligi = tr.page.title()[:60]
        except Exception:
            sayfa_basligi = "?"
        if s == 1:
            # İlk sayfanın boş gelmesi engelleme belirtisi olabilir: 24 saat değil 2 saat sonra yeniden dene
            ertele = (datetime.now() - timedelta(hours=max(saat - 2, 0))).isoformat(timespec="seconds")
            for s2 in range(1, n + 1):  # sonraki sayfalar da ilk sayfayla birlikte denensin
                dset(con, f"arama:{t['id']}:{sorgu}:{s2}", ertele)
            log(f"  arama '{sorgu}' s1: sonuç yok (sayfa: {sayfa_basligi}); 2 saat sonra yeniden denenecek")
            return
        for s2 in range(s + 1, n + 1):  # sonraki sayfalar da boştur
            dset(con, f"arama:{t['id']}:{sorgu}:{s2}", simdi_s())
        log(f"  arama '{sorgu}' s{s}: sonuç yok (sayfa: {sayfa_basligi})")
        return
    if t.get("beden") or t.get("saat"):
        gun = int(t.get("yeniden_tarama_gun", 7))
        ust = float(t["saat"]["fiyat_ust"]) if t.get("saat") else None
        eklenen = 0
        for r in sonuc:
            fiyat = T.parse_price(r["fiyat"])
            if ust is not None and fiyat is not None and fiyat > ust:
                continue
            if T.baslik_uygun(r["baslik"], t) and not T.yakin_zamanda_tarandi(con, r["asin"], gun):
                eklenen += con.execute("INSERT OR IGNORE INTO adaylar VALUES(?,?,?,?)",
                                       (r["asin"], t["id"], r["baslik"], simdi_s())).rowcount
        con.commit()
        log(f"  arama '{sorgu}' s{s}: {len(sonuc)} sonuç, {eklenen} yeni aday")
    else:
        kayit = 0
        for r in sonuc:
            if not T.baslik_uygun(r["baslik"], t):
                continue
            if con.execute("SELECT 1 FROM urunler WHERE asin=? AND aktif=0", (r["asin"],)).fetchone():
                continue
            fiyat = T.parse_price(r["fiyat"])
            if fiyat is None:
                continue
            etiket = T.parfum_etiketi(r["baslik"], t["parfum"]) if t.get("parfum") else None
            T.kaydet(con, t, a, olaylar, asin=r["asin"], parent=None, baslik=r["baslik"],
                     beden=etiket or None, fiyat=fiyat, liste=T.parse_price(r["liste"]), stokta=True)
            kayit += 1
        log(f"  arama '{sorgu}' s{s}: {len(sonuc)} sonuç, {kayit} fiyat kaydedildi")


def gorev_kesif(tr, con, cfg, a, o, olaylar) -> bool:
    while True:
        r = con.execute("SELECT asin,takip_id,baslik FROM adaylar ORDER BY eklenme LIMIT 1").fetchone()
        if not r:
            return False
        con.execute("DELETE FROM adaylar WHERE asin=?", (r["asin"],))
        con.commit()
        t = takip_bul(cfg, r["takip_id"])
        if t and (t.get("beden") or t.get("saat")) and not T.yakin_zamanda_tarandi(
                con, r["asin"], int(t.get("yeniden_tarama_gun", 7))):
            break
    log(f"  keşif: {r['baslik'][:60]}")
    if t.get("saat"):
        T.guvenli(_kesfet_saat, tr, con, t, a, olaylar, r["asin"])
    else:
        T.guvenli(T._kesfet, tr, con, t, a, olaylar, r["asin"], t["beden"],
                  int(t.get("yeniden_tarama_gun", 7)))
    return True


GOREVLER = {"kontrol": gorev_kontrol, "arama": gorev_arama, "kesif": gorev_kesif}
SIRA = ["kontrol", "kontrol", "arama", "kesif"]
SIRA_KESIF = ["arama", "kesif"]  # elle "kesif" modu: kontrol yok, önce aramalar ve yeni ürünler
MAX_ELLE_BUTCE = 120


# ---------------------------------------------------------------- web sayfası (sade)

def _gecmis_ozet(con) -> dict:
    """Her ürün için stoktayken görülen en ucuz, en pahalı ve fiyat değişim sırası."""
    oz: dict = {}
    for r in con.execute("SELECT asin, fiyat FROM gecmis WHERE fiyat IS NOT NULL AND stokta=1 "
                         "ORDER BY asin, ts"):
        d = oz.setdefault(r["asin"], {"min": r["fiyat"], "max": r["fiyat"], "seri": []})
        d["min"] = min(d["min"], r["fiyat"])
        d["max"] = max(d["max"], r["fiyat"])
        if not d["seri"] or d["seri"][-1] != r["fiyat"]:  # art arda aynı fiyatları tek say
            d["seri"].append(r["fiyat"])
    return oz


def _degisim(r, d) -> str:
    if not r["stokta"] or not d or len(d["seri"]) < 2 or d["seri"][-1] != r["son_fiyat"]:
        return ""
    onceki, son = d["seri"][-2], d["seri"][-1]
    p = (son - onceki) * 100 / onceki
    yuzde = f"{abs(p):.1f}".replace(".", ",")
    if p < 0:
        return f'<span class="d">↓ %{yuzde}</span>'
    return f'<span class="z">↑ %{yuzde}</span>'


def site_uret(con, cfg) -> None:
    SITE.mkdir(exist_ok=True)
    e = html.escape
    oz = _gecmis_ozet(con)
    son = con.execute("SELECT * FROM calismalar ORDER BY rowid DESC LIMIT 1").fetchone()
    parca = [f"<h1>Amazon fiyat takibi</h1><p>Son güncelleme: {datetime.now():%d.%m.%Y %H:%M}<br>"
             "<small>Değişim: son fiyatın bir önceki farklı fiyata göre farkı. "
             "En ucuz / en pahalı: takip başladığından beri stoktayken görülen fiyatlar.</small></p>"]
    for t in cfg["takipler"]:
        if not t.get("aktif", True):
            continue
        rows = con.execute("SELECT * FROM urunler WHERE takip_id=? AND aktif=1 "
                           "ORDER BY stokta DESC, son_fiyat", (t["id"],)).fetchall()
        gizli = ""
        if t.get("saat"):
            ust = float(t["saat"]["fiyat_ust"])
            tum = len(rows)
            rows = [r for r in rows if r["satici_uygun"] != 0 and (not r["stokta"] or (r["son_fiyat"] or 0) <= ust)]
            if tum > len(rows):
                gizli = (f"<p><small>{tum - len(rows)} saat şu an gizli: fiyatı {tl(ust)} üstünde "
                         "ya da satış kutusundaki satıcı ölçütü karşılamıyor. Koşul düzelince yeniden görünür.</small></p>")
        parca.append(f"<h2>{e(t['ad'])} <small>({len(rows)})</small></h2>{gizli}")
        if not rows:
            parca.append("<p>Henüz ürün yok.</p>")
            continue
        parca.append('<div class="t"><table><thead><tr><th>Son fiyat</th><th>Değişim</th>'
                     '<th>En ucuz</th><th>En pahalı</th><th class="u">Ürün</th></tr></thead><tbody>')
        for r in rows:
            d = oz.get(r["asin"])
            url = f"{T.BASE}/dp/{r['asin']}" + ("?th=1&psc=1" if r["beden"] else "")
            fiyat = f"<b>{tl(r['son_fiyat'])}</b>" if r["stokta"] else "stokta yok"
            ek = f" [{e(r['beden'])}]" if r["beden"] else ""
            parca.append(f"<tr><td>{fiyat}</td><td>{_degisim(r, d)}</td>"
                         f"<td>{tl(d['min']) if d else '-'}</td><td>{tl(d['max']) if d else '-'}</td>"
                         f'<td class="u"><a href="{e(url)}">{e(r["baslik"] or r["asin"])}</a>{ek}</td></tr>')
        parca.append("</tbody></table></div>")
    if son:
        parca.append(f"<p><small>Son çalışma: {e(son['ts'][:16].replace('T', ' '))}, "
                     f"{son['sayfa']} sayfa, sonuç: {e(son['sonuc'])}</small></p>")
    css = (":root{color-scheme:light dark}body{font-family:-apple-system,sans-serif;max-width:1100px;"
           "margin:0 auto;padding:12px;line-height:1.45}.t{overflow-x:auto}"
           "table{border-collapse:collapse;width:100%;font-size:14px}"
           "th,td{padding:5px 8px;border-bottom:1px solid #8884;text-align:right;white-space:nowrap;vertical-align:top}"
           "th{font-weight:600}.u{text-align:left;white-space:normal;min-width:240px}"
           ".d{color:#2da44e;font-weight:600}.z{color:#e5534b;font-weight:600}")
    sayfa = ('<!doctype html><html lang="tr"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width, initial-scale=1">'
             '<meta name="robots" content="noindex"><title>Amazon fiyat takibi</title>'
             f'<style>{css}</style></head><body>{"".join(parca)}</body></html>')
    (SITE / "index.html").write_text(sayfa, encoding="utf-8")


# ---------------------------------------------------------------- ana akış

def veri_sil(con, tid: str) -> None:
    """config'deki veri_sil listesindeki takibin tüm kayıtlarını siler (tekrar çalışırsa iş yapmaz)."""
    n = con.execute("SELECT COUNT(*) FROM urunler WHERE takip_id=?", (tid,)).fetchone()[0]
    con.execute("DELETE FROM gecmis WHERE asin IN (SELECT asin FROM urunler WHERE takip_id=?)", (tid,))
    for tablo in ("urunler", "taranan", "adaylar"):
        con.execute(f"DELETE FROM {tablo} WHERE takip_id=?", (tid,))
    con.execute("DELETE FROM durum WHERE anahtar LIKE ? OR anahtar=?", (f"arama:{tid}:%", f"markasiz:{tid}"))
    con.commit()
    if n:
        log(f"'{tid}' takibinin {n} ürünü ve fiyat geçmişi silindi")


def main() -> None:
    cfg = T.ayarlari_yukle()
    a = cfg["ayarlar"]
    o = {**ONLINE_VARSAYILAN, **(cfg.get("online") or {})}
    # Elle başlatılan çalışmada GitHub'daki "Run workflow" kutusundan gelen değerler
    elle = os.environ.get("SAYFA_BUTCESI", "").strip()
    if elle.isdigit() and int(elle) > 0:
        o["sayfa_butcesi"] = min(int(elle), MAX_ELLE_BUTCE)
    mod = os.environ.get("MOD", "").strip() or "normal"
    sira = SIRA_KESIF if mod == "kesif" else SIRA
    log(f"Mod: {mod}, sayfa bütçesi: {o['sayfa_butcesi']}")
    con = T.db_baglan()
    con.executescript(EK_SCHEMA)
    sema_guncelle(con)
    for tid in cfg.get("veri_sil") or []:
        veri_sil(con, tid)
    for t in cfg["takipler"]:
        if not t.get("saat"):
            T.filtreyi_uygula(con, t)

    olaylar: list = []
    sonuc = "tamam"
    tr = None
    try:
        tr = OnlineTarayici(o)
        while tr.kalan() > 0:
            yapildi = False
            for tip in sira:
                if tr.kalan() <= 0:
                    break
                yapildi = GOREVLER[tip](tr, con, cfg, a, o, olaylar) or yapildi
            if not yapildi:
                log("Yapılacak iş kalmadı.")
                break
    except T.DogrulamaGerekli as ex:
        sonuc = "captcha"
        log(f"CAPTCHA yeniden denemede de çıktı ({ex}); bu çalışma burada bitti.")
    except T.LimitAsildi:
        sonuc = "limit"
    except Exception as ex:
        sonuc = "hata"
        log(f"HATA: {ex!r}")
    finally:
        if tr:
            tr.kapat()
        con.execute("INSERT INTO calismalar VALUES(?,?,?,?)",
                    (simdi_s(), tr.istek if tr else 0, tr.captcha if tr else 0, sonuc))
        con.commit()
        log(f"Bitti: {tr.istek if tr else 0} sayfa, {tr.captcha if tr else 0} CAPTCHA, sonuç: {sonuc}")
    site_uret(con, cfg)
    con.close()


if __name__ == "__main__":
    main()
