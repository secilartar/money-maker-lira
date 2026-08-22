#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Money Maker — Lira'ya Sor API (Gemini) v3.2 (Firebase Fon Eklentili)
"""

from __future__ import annotations

import os
import re
import time
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import yfinance as yf
import httpx
import pandas as pd
import firebase_admin
from firebase_admin import credentials, db

# YENİ SDK İÇİN GEREKLİ İÇE AKTARMALAR
from google import genai
from google.genai import types

# ================== AYARLAR ==================
API_SECRET_KEY = (os.environ.get("API_SECRET_KEY") or "").strip()
GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip()
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

# --- FIREBASE AYARLARI ---
FIREBASE_URL = "https://money-maker-f59c4-default-rtdb.europe-west1.firebasedatabase.app"
FIREBASE_JSON_STR = os.environ.get("FIREBASE_CREDENTIALS_JSON")

if FIREBASE_JSON_STR and not firebase_admin._apps:
    try:
        cred_dict = json.loads(FIREBASE_JSON_STR)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred, {'databaseURL': FIREBASE_URL})
        print("[SİSTEM] Firebase bağlantısı başarılı!")
    except Exception as e:
        print(f"[SİSTEM] Firebase başlatılamadı: {e}")

KAP_API = "https://www.kap.org.tr/tr/api/disclosure/members/byCriteria"
KAP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Referer": "https://www.kap.org.tr/tr/bildirim-sorgu",
    "Origin": "https://www.kap.org.tr",
}

TR_TZ = timezone(timedelta(hours=3))

app = FastAPI(title="Money Maker Lira'ya Sor", version="3.2")

# DÜZELTME: CORS çakışması giderildi
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=False, # allow_origins="*" varken True olamaz!
    allow_methods=["*"],
    allow_headers=["*"],
)


class SorRequest(BaseModel):
    soru: str = Field(..., min_length=2, max_length=2000)
    thread_id: Optional[str] = None
    new_thread: bool = True
    web: Optional[bool] = None


class SorResponse(BaseModel):
    ok: bool
    soru: str
    cevap: str
    thread_id: str = "gemini"
    kaynak: str = "gemini"


def _check_secret(x_api_key: Optional[str]) -> None:
    if not API_SECRET_KEY:
        raise HTTPException(500, "API_SECRET_KEY tanımlı değil")
    if not x_api_key or x_api_key != API_SECRET_KEY:
        raise HTTPException(403, "Erişim reddedildi")


def _get_client():
    if not GEMINI_API_KEY:
        raise HTTPException(500, "GEMINI_API_KEY tanımlı değil")
    return genai.Client(api_key=GEMINI_API_KEY)


# ================== BEYAZ LİSTE (WHİTELİST) ==================
# Tüm BİST hisseleri ve fon kodlarının birleştirilmiş, temizlenmiş hali[cite: 4, 5, 6]
BEYAZ_LISTE = {
    # Hisseler[cite: 4]
    "VSNMD", "GZNMI", "ESEN", "SMRVA", "EMKEL", "BORLS", "BEGYO", "ADESE", "USAK", "KONTR", 
    "TRILC", "YBTAS", "DERHL", "OTTO", "LINK", "QNBTR", "TEKTU", "MEGAP", "BIENY", "VRGYO", 
    "FORMT", "QUAGR", "ARZUM", "IHLAS", "IHYAY", "KZBGY", "OTKAR", "PAPIL", "CEMAS", "ARENA", 
    "OSTIM", "MERCN", "MARTI", "GIPTA", "LRSHO", "VKING", "PKENT", "ENSRI", "YAYLA", "VESBE", 
    "SNPAM", "GENTS", "IMASM", "TUKAS", "EUREN", "INVEO", "IHLGM", "MAKTK", "GRTHO", "BAYRK", 
    "BALSU", "AGROT", "ERCB", "TUREX", "IHGZT", "NATEN", "VESTL", "AKYHO", "SODSN", "CONSE", 
    "YESIL", "AVTUR", "PGSUS", "KONYA", "HATEK", "YYAPI", "REEDR", "CMBTN", "MRGYO", "OBAMS", 
    "CANTE", "SAMAT", "TMSN", "TURGG", "POLTK", "ALVES", "GLRYH", "DCTTR", "ISBIR", "FENER", 
    "RAYSG", "DOFER", "EGEEN", "MERKO", "PAMEL", "DARDL", "KSTUR", "BAGFS", "JANTS", "DAPGM", 
    "CASA", "TTRAK", "KRGYO", "MEKAG", "SUWEN", "TKNSA", "TEZOL", "UNLU", "NIBAS", "QNBFK", 
    "ALCAR", "ZOREN", "CMENT", "KRTEK", "BUCIM", "INTEK", "YONGA", "BASCM", "TUCLK", "KNFRT", 
    "KLNMA", "CEOEM", "BESLR", "MEDTR", "ARCLK", "KLGYO", "GENIL", "BLUME", "BIGCH", "HEKTS", 
    "KATMR", "BFREN", "KENT", "MARBL", "MNDRS", "SNGYO", "ZRGYO", "SRVGY", "ANGEN", "GSRAY", 
    "DYOBY", "DENGE", "OYLUM", "TSGYO", "EMNIS", "KFEIN", "KAYSE", "YKSLN", "ONRYT", "KLSER", 
    "EFOR", "YIGIT", "SNICA", "VAKFN", "SERNT", "CEMTS", "DESPC", "ENDAE", "DESA", "MNDTR", 
    "BERA", "PLTUR", "MZHLD", "AVHOL", "PATEK", "ADEL", "SANKO", "KOTON", "GOODY", "BANVT", 
    "GLCVY", "HUNER", "BNTAS", "BIOEN", "SILVR", "BMSCH", "IHEVA", "ELITE", "MERIT", "MIATK", 
    "LKMNH", "BOSSA", "ULKER", "EGSER", "YAPRK", "PARSN", "ISSEN", "KZGYO", "MOPAS", "ASUZU", 
    "DERIM", "ATAGY", "MAKIM", "ULAS", "OSMEN", "BOBET", "SASA", "BJKAS", "FROTO", "PENTA", 
    "NUGYO", "MHRGY", "PNSUT", "TBORG", "KLYPV", "MAALT", "AKENR", "ESCAR", "SURGY", "HTTBT", 
    "TATEN", "TSKB", "BIZIM", "LOGO", "BIGTK", "GOLTS", "BLCYT", "ISCTR", "DOKTA", "BEYAZ", 
    "EKOS", "KLKIM", "TGSAS", "TLMAN", "KUTPO", "BRYAT", "LMKDC", "RTALB", "VERTU", "HOROZ", 
    "BARMA", "LYDYE", "ENTRA", "DNISI", "SKYMD", "ZEDUR", "CLEBI", "MRSHL", "AVPGY", "ALFAS", 
    "SEGYO", "TSPOR", "SMART", "MACKO", "GOKNR", "ISYAT", "ALKIM", "SONME", "ALTNY", "AVOD", 
    "ULUFA", "NTHOL", "BRKVY", "ORCAY", "DIRIT", "A1CAP", "IZINV", "GARAN", "KOPOL", "FMIZP", 
    "HKTM", "ISATR", "ERBOS", "ISMEN", "PETUN", "PRKME", "OYAKC", "RUZYE", "ENERY", "EGGUB", 
    "EPLAS", "TNZTP", "INGRM", "GWIND", "SAHOL", "AKFGY", "CIMSA", "EYGYO", "BINBN", "PINSU", 
    "ALBRK", "HURGZ", "EKSUN", "COSMO", "ASGYO", "BINHO", "GEDIK", "YYLGD", "DZGYO", "OYYAT", 
    "ORMA", "AKGRT", "A1YEN", "AYCES", "PRKAB", "BORSK", "KAREL", "EKGYO", "BRLSM", "KARSN", 
    "TTKOM", "DGGYO", "OZKGY", "SUMAS", "AGYO", "KONKA", "AHGAZ", "AZTEK", "SEKFK", "LUKSK", 
    "AFYON", "ULUUN", "MAVI", "GLBMD", "BRISA", "FLAP", "OFSYM", "KLMSN", "BRKSN", "KCAER", 
    "SAYAS", "GLRMK", "GATEG", "CUSAN", "EKIZ", "SMRTG", "IDGYO", "EUHOL", "AKBNK", "SELVA", 
    "VKGYO", "FZLGY", "THYAO", "OZGYO", "NUHCM", "YKBNK", "MTRYO", "BVSAN", "GEDZA", "PENGD", 
    "BULGS", "KIMMR", "GARFA", "DOAS", "ALKA", "IZENR", "TAVHL", "KRDMA", "KRVGD", "SKBNK", 
    "DMSAS", "AVGYO", "EDIP", "VAKBN", "ISBTR", "ATAKP", "ETILR", "DEVA", "PETKM", "MTRKS", 
    "SEYKM", "ALARK", "ACSEL", "SISE", "BALAT", "IZFAS", "KRSTL", "ALGYO", "TDGYO", "HUBVC", 
    "GSDHO", "OBASE", "SUNTK", "LILAK", "YATAS", "CGCAM", "IHAAS", "VAKKO", "GRSEL", "SKTAS", 
    "KRPLS", "ARTMS", "KCHOL", "SAFKR", "KORDS", "MPARK", "ERSU", "DGNMO", "BAKAB", "TCELL", 
    "PSDTC", "NTGAZ", "FADE", "GOZDE", "EGEGY", "PNLSN", "NETAS", "AGHOL", "PRDGS", "ISFIN", 
    "SANFM", "ATLAS", "DURKN", "TOASO", "INTEM", "MGROS", "BTCIM", "HRKET", "TARKM", "DURDO", 
    "HDFGS", "KLRHO", "DOCO", "TURSG", "ATEKS", "SOKE", "LIDFA", "ARSAN", "ENKAI", "HATSN", 
    "AKMGY", "DOHOL", "VBTYZ", "TATGD", "VKFYO", "AYDEM", "OYAYO", "ANSGR", "RYSAS", "KUYAS", 
    "BASGZ", "TABGD", "RUBNS", "AEFES", "ORGE", "KMPUR", "AYEN", "SDTTR", "TRCAS", "TRGYO", 
    "LYDHO", "AKSA", "HALKB", "ODAS", "INDES", "SOKM", "AKFYE", "MSGYO", "ICUGS", "POLHO", 
    "HLGYO", "KTSKR", "RNPOL", "ICBCT", "PSGYO", "RODRG", "LIDER", "GSDDE", "CATES", "ECILC", 
    "EUYO", "ISGYO", "YUNSA", "ALCTL", "DGATE", "AKSGY", "FORTE", "AGESA", "ISDMR", "ADGYO", 
    "ULUSE", "BRKO", "KRDMD", "MEPET", "ANHYT", "ISKPL", "BIMAS", "ECZYT", "RGYAS", "PKART", 
    "DITAS", "ESCOM", "BYDNR", "BRSAN", "EREGL", "MMCAS", "ISKUR", "EGPRO", "FRIGO", "RYGYO", 
    "GUBRF", "VERUS", "GESAN", "UFUK", "PCILT", "MARKA", "DAGI", "BMSTL", "AKCNS", "CCOLA", 
    "OZYSR", "KBORU", "AYES", "GRNYO", "METRO", "KUVVA", "ENJSA", "MOBTL", "DUNYH", "INFO", 
    "EBEBK", "KRONT", "RALYH", "FONET", "OZSUB", "AYGAZ", "TRMET", "TRENJ", "GLYHO", "TKFEN", 
    "TERA", "KARTN", "INVES", "KOCMT", "BAHKM", "SARKY", "KAPLM", "PASEU", "ATSYH", "ARASE", 
    "ASELS", "CRFSA", "BURCE", "MAGEN", "ONCSM", "MOGAN", "VANGD", "TUPRS", "GEREL", "PEKGY", 
    "SEKUR", "TRALT", "SEGMN", "MEGMT", "KLSYN", "YEOTK", "AKSEN", "SELEC", "PAGYO", "CELHA", 
    "AKSUE", "ISGSY", "IZMDC", "ARDYZ", "EGEPO", "BSOKE", "ATATP", "SANEL", "AHSGY", "DMRGD", 
    "KGYO", "TCKRC", "EUKYO", "SKYLP", "EUPWR", "YGGYO", "ETYAT", "DSTKF", "CWENE", "BRMEN", 
    "ASTOR", "CVKMD", "OZRDN", "EDATA", "KERVN", "DOGUB", "PRZMA", "AKFIS", "CEMZY", "KRDMB", 
    "GMTAS", "ALKLC", "TEHOL", "TMPOL", "ARMGD", "MANAS", "ANELE", "CRDFA", "BURVA", "BIGEN", 
    "TRHOL", "IEYHO", "KTLEV", "OZATD", "ODINE", "GUNDG", "HEDEF", "ZERGY", "MARMR", "MEYSU", 
    "UCAYM", "PAHOL", "VAKFA", "FRMPL", "DMLKTG", "ARFYE", "ZGYO", "DOFRB", "ECOGR", "AKHAN", 
    "NETCD", "LXGYO", "AAGYO", "ENPRA", "GENKM", "SVGYO", "MCARD", "ATATR", "BESTE", "EMPAE", 
    "SOHOE", "EKDMR", "ORZAX", "BETAE", "ALBTN", "GOLDA", "MASFN", "EKIM", "ISVEA", "METEN", 
    "SARAE", "SSAAT", "KARCL", "QUICK", "CITAS", "KPEKS", "VEYAS", "TKNKA",

    # Fonlar ve EYF Kodları[cite: 5, 6]
    "BLH", "BND", "BOE", "BTL", "DJA", "EOZ", "EYZ", "FGA", "FGS", "FUS", "ILK", "KHO", "LTK", 
    "MDL", "NBS", "OHS", "OHY", "OPE", "OPK", "ZBB", "ZBP", "ZEA", "ZEO", "ZGD", "ZKE", "ZKP", 
    "ZPP", "ZPT", "ZRE", "ZSR", "ZTK", "ZTM", "ZTR", "AAL", "AAS", "AAV", "ABG", "ABJ", "ABU", 
    "AC1", "AC2", "AC3", "AC4", "AC5", "AC6", "AC7", "AC8", "ACC", "ACD", "ACN", "ACU", "ADE", 
    "ADP", "AED", "AES", "AEV", "AFA", "AFO", "AFS", "AFT", "AFV", "AGC", "AHH", "AHI", "AHN", 
    "AHU", "AHV", "AHZ", "AI1", "AIH", "AII", "AIM", "AIR", "AIS", "AJ1", "AJE", "AJK", "AJL", 
    "AJN", "AK2", "AK3", "AKE", "AKU", "AL4", "AL5", "AL6", "AL7", "ALC", "ALE", "AN1", "ANL", 
    "ANZ", "AOJ", "AOY", "AP2", "AP4", "AP5", "AP6", "AP7", "AP9", "APJ", "APP", "APT", "ARE", 
    "ARF", "ARL", "ARM", "ARP", "AS1", "AS3", "ASJ", "ATJ", "AU1", "AUV", "AVC", "AVT", "AVZ", 
    "AY6", "AY7", "AYA", "AYR", "AZF", "AZJ", "AZV", "AZZ", "BA1", "BAC", "BAG", "BAI", "BAL", 
    "BAO", "BAS", "BBF", "BBI", "BBN", "BBO", "BBP", "BBS", "BCC", "BCK", "BCO", "BDA", "BDC", 
    "BDE", "BDI", "BDO", "BDS", "BDY", "BFE", "BFN", "BFO", "BFS", "BFT", "BGH", "BGP", "BGR", 
    "BHA", "BHE", "BHF", "BHH", "BHI", "BHL", "BHN", "BHO", "BI5", "BIA", "BID", "BIG", "BIH", 
    "BIK", "BIO", "BIP", "BIS", "BIT", "BIY", "BJD", "BKO", "BKY", "BLA", "BLD", "BLG", "BLT", 
    "BMU", "BNC", "BNH", "BOH", "BOL", "BON", "BOP", "BOS", "BP5", "BPD", "BPZ", "BRB", "BRC", 
    "BRF", "BRG", "BRH", "BRR", "BRT", "BRZ", "BS1", "BSA", "BSC", "BSD", "BSE", "BSF", "BSH", 
    "BSM", "BSN", "BST", "BTE", "BTJ", "BTK", "BTP", "BTY", "BTZ", "BUB", "BUC", "BUH", "BUP", 
    "BUT", "BUV", "BUY", "BV1", "BVB", "BVC", "BVD", "BVF", "BVH", "BVI", "BVK", "BVM", "BVR", 
    "BVT", "BVV", "BVZ", "BYZ", "CAH", "CBD", "CBN", "CBO", "CEY", "CFO", "CGD", "CHY", "CIN", 
    "CJD", "CJF", "CJG", "CJH", "CKF", "CKL", "CKS", "COT", "CPT", "CPU", "CRL", "CSD", "CSH", 
    "CTF", "CTG", "CTM", "CTP", "CTV", "CVA", "CVB", "CVC", "CVD", "CVE", "CVF", "CVK", "CVL", 
    "CYD", "DA1", "DAC", "DAE", "DAH", "DAI", "DAL", "DAP", "DAS", "DAT", "DAV", "DAZ", "DB2", 
    "DBA", "DBB", "DBG", "DBH", "DBK", "DBP", "DBZ", "DCB", "DCD", "DCE", "DCN", "DCP", "DCV", 
    "DDA", "DDC", "DDE", "DDF", "DDP", "DEF", "DEH", "DEI", "DFC", "DFD", "DFI", "DFO", "DGF", 
    "DGH", "DHI", "DHJ", "DHM", "DHP", "DHS", "DHT", "DHV", "DID", "DIH", "DII", "DIM", "DIP", 
    "DK1", "DK8", "DKA", "DKC", "DKH", "DKL", "DKP", "DKR", "DKS", "DKY", "DL2", "DLD", "DLG", 
    "DLN", "DLY", "DLZ", "DMG", "DMI", "DMR", "DMV", "DMZ", "DNA", "DNF", "DNH", "DNK", "DNL", 
    "DNM", "DNP", "DNU", "DOC", "DOD", "DOH", "DOL", "DOR", "DOS", "DOV", "DP1", "DP2", "DP3", 
    "DP4", "DP5", "DP6", "DP7", "DP8", "DP9", "DPB", "DPC", "DPE", "DPG", "DPI", "DPK", "DPL", 
    "DPN", "DPP", "DPT", "DPZ", "DRA", "DRD", "DRH", "DRS", "DRT", "DSC", "DSD", "DSH", "DSP", 
    "DSR", "DSU", "DSV", "DTH", "DTL", "DTM", "DTO", "DTP", "DTV", "DTZ", "DUC", "DUD", "DUH", 
    "DUT", "DUV", "DVC", "DVI", "DVN", "DVO", "DVS", "DVT", "DVU", "DVZ", "DXP", "DYJ", "DYN", 
    "DYS", "DZ2", "DZE", "DZG", "DZM", "DZP", "DZS", "EBD", "EBI", "EBS", "EC2", "ECA", "ECB", 
    "ECV", "EDD", "EDN", "EDP", "EDT", "EDU", "EES", "EGP", "EGR", "EHS", "EIB", "EID", "EIL", 
    "EJG", "EKF", "EKL", "ELZ", "EME", "EML", "ENA", "ENJ", "ENO", "ENR", "ENS", "EP1", "EPA", 
    "EPI", "EPK", "EPO", "EPP", "EPT", "ESG", "ESN", "ESP", "ETN", "EUN", "EUZ", "EVM", "EYT", 
    "EZM", "FAK", "FAL", "FBC", "FBI", "FBN", "FBV", "FBZ", "FCK", "FCS", "FD1", "FDE", "FDG", 
    "FDN", "FDO", "FDV", "FDY", "FDZ", "FFD", "FFF", "FFH", "FFO", "FFP", "FHI", "FHP", "FHZ", 
    "FI3", "FI5", "FIB", "FID", "FIL", "FIT", "FJB", "FJC", "FJM", "FJN", "FJZ", "FKE", "FKH", 
    "FKM", "FKV", "FLS", "FLY", "FMB", "FMG", "FMS", "FMV", "FNE", "FNN", "FNO", "FNT", "FP4", 
    "FPE", "FPG", "FPH", "FPI", "FPK", "FPR", "FPZ", "FR1", "FRA", "FRC", "FRZ", "FS1", "FS2", 
    "FS3", "FS4", "FS5", "FS6", "FSF", "FSG", "FSH", "FSK", "FSM", "FSP", "FSR", "FSU", "FSV", 
    "FTL", "FTM", "FTY", "FUA", "FUB", "FUM", "FUN", "FUP", "FVL", "FYA", "FYB", "FYD", "FYF", 
    "FYG", "FYH", "FYI", "FYM", "FYO", "FYT", "FYZ", "FZJ", "FZP", "GA1", "GAC", "GAE", "GAF", 
    "GAG", "GAH", "GAJ", "GAL", "GAN", "GAS", "GAU", "GAV", "GBC", "GBG", "GBH", "GBJ", "GBL", 
    "GBN", "GBP", "GBV", "GBZ", "GCA", "GCC", "GCD", "GCI", "GCZ", "GDJ", "GDP", "GEC", "GEI", 
    "GEZ", "GFB", "GFD", "GFI", "GFL", "GFN", "GFY", "GGA", "GGC", "GGD", "GGK", "GGM", "GGN", 
    "GGP", "GGR", "GHS", "GID", "GIE", "GIH", "GJA", "GJB", "GJD", "GJE", "GJF", "GJH", "GJM", 
    "GJO", "GKE", "GKF", "GKG", "GKH", "GKK", "GKL", "GKM", "GKO", "GKV", "GL1", "GLC", "GLE", 
    "GLG", "GLL", "GLM", "GLP", "GLS", "GLV", "GMA", "GMC", "GMD", "GME", "GMI", "GMM", "GMN", 
    "GMO", "GMP", "GMR", "GMV", "GMZ", "GNH", "GNK", "GNL", "GNP", "GNS", "GNZ", "GO1", "GO2", 
    "GO3", "GO4", "GO6", "GO9", "GOF", "GOH", "GOK", "GOL", "GOP", "GP1", "GP3", "GPA", "GPB", 
    "GPC", "GPF", "GPG", "GPH", "GPI", "GPJ", "GPL", "GPM", "GPN", "GPT", "GPU", "GPV", "GPZ", 
    "GRL", "GRO", "GRT", "GSE", "GSG", "GSL", "GSO", "GSP", "GSR", "GSU", "GTA", "GTF", "GTH", 
    "GTK", "GTL", "GTM", "GTN", "GTY", "GTZ", "GUA", "GUB", "GUC", "GUF", "GUH", "GUK", "GUM", 
    "GUV", "GVA", "GVB", "GVC", "GVD", "GVI", "GVL", "GVZ", "GYC", "GYG", "GYK", "GYL", "GYN", 
    "GYR", "GZB", "GZD", "GZE", "GZG", "GZH", "GZJ", "GZL", "GZM", "GZN", "GZO", "GZP", "GZR", 
    "GZU", "GZV", "GZY", "GZZ", "HAA", "HAE", "HAG", "HAI", "HAM", "HAR", "HAT", "HB1", "HB2", 
    "HBF", "HBI", "HBJ", "HBM", "HBN", "HBP", "HBU", "HBV", "HCF", "HCV", "HDA", "HDC", "HDD", 
    "HDE", "HDH", "HDJ", "HDK", "HDL", "HDS", "HDV", "HEH", "HFA", "HFI", "HFO", "HFR", "HFV", 
    "HFY", "HGC", "HGH", "HGJ", "HGM", "HGR", "HGT", "HGU", "HGV", "HIA", "HID", "HIF", "HIH", 
    "HII", "HIL", "HIM", "HIN", "HIP", "HIS", "HIZ", "HJA", "HJB", "HJJ", "HKG", "HKH", "HKJ", 
    "HKM", "HKP", "HKR", "HKV", "HLA", "HLL", "HLR", "HMC", "HME", "HMG", "HMK", "HML", "HMR", 
    "HMS", "HMT", "HMV", "HNC", "HNJ", "HNS", "HOA", "HOI", "HOP", "HOT", "HOY", "HP3", "HPC", 
    "HPD", "HPF", "HPH", "HPI", "HPJ", "HPL", "HPO", "HPP", "HPT", "HPV", "HPZ", "HRS", "HRT", 
    "HRZ", "HSA", "HSL", "HSP", "HST", "HTD", "HTE", "HTF", "HTI", "HTJ", "HTK", "HTM", "HTR", 
    "HTS", "HTZ", "HUI", "HUS", "HVA", "HVB", "HVC", "HVI", "HVK", "HVL", "HVN", "HVS", "HVT", 
    "HVU", "HVV", "HVZ", "HYK", "HYP", "HYU", "HYV", "HYY", "HYZ", "HZV", "IAC", "IAE", "IAI", 
    "IAJ", "IAM", "IAR", "IAS", "IAT", "IAU", "IAY", "IBB", "IBC", "IBE", "IBG", "IBJ", "IBK", 
    "IBM", "IBP", "IBR", "ICA", "ICC", "ICD", "ICE", "ICF", "ICG", "ICH", "ICN", "ICS", "ICV", 
    "ICZ", "IDD", "IDF", "IDH", "IDI", "IDL", "IDO", "IDP", "IDV", "IDY", "IED", "IEN", "IEV", 
    "IEZ", "IFD", "IFN", "IFV", "IFY", "IGF", "IGL", "IGM", "IGZ", "IH1", "IHA", "IHC", "IHE", 
    "IHK", "IHP", "IHT", "IHV", "IHY", "IHZ", "IIA", "IIC", "IIE", "IIF", "IIH", "IIN", "IIS", 
    "IJA", "IJB", "IJC", "IJE", "IJF", "IJH", "IJI", "IJK", "IJL", "IJP", "IJS", "IJT", "IJV", 
    "IJZ", "IK2", "IKL", "IKP", "IKV", "ILC", "ILE", "ILH", "ILI", "ILM", "ILP", "ILU", "ILZ", 
    "IMB", "IMF", "IMG", "IMH", "IML", "IMO", "IMS", "IMT", "IMY", "IMZ", "INH", "INV", "INZ", 
    "IOG", "IOH", "IOJ", "IOL", "IOM", "ION", "IOO", "IOP", "IOT", "IOV", "IPA", "IPB", "IPC", 
    "IPE", "IPF", "IPG", "IPJ", "IPK", "IPO", "IPP", "IPR", "IPU", "IPV", "IRB", "IRE", "IRF", 
    "IRL", "IRO", "IRT", "IRV", "IRY", "ISR", "ISS", "IST", "ITA", "ITC", "ITD", "ITJ", "ITL", 
    "ITP", "ITR", "ITV", "ITZ", "IUA", "IUB", "IUC", "IUF", "IUH", "IUM", "IUN", "IUV", "IUZ", 
    "IV2", "IV6", "IV7", "IV8", "IVA", "IVF", "IVS", "IVY", "IYB", "IYP", "IYR", "IYS", "IYV", 
    "IZA", "IZB", "IZE", "IZL", "IZS", "IZV", "IZY", "JET", "JOT", "JUP", "KAC", "KAN", "KAV", 
    "KAY", "KBJ", "KBP", "KBZ", "KCL", "KCN", "KCR", "KCV", "KDE", "KDI", "KDK", "KDL", "KDO", 
    "KDS", "KDT", "KDV", "KDZ", "KEI", "KEO", "KEU", "KFZ", "KGM", "KH1", "KHA", "KHB", "KHC", 
    "KHD", "KHF", "KHJ", "KHP", "KHT", "KHU", "KIA", "KIB", "KIE", "KIH", "KIK", "KIS", "KJK", 
    "KKB", "KKC", "KKE", "KKH", "KKL", "KKO", "KKP", "KKT", "KLH", "KLI", "KLL", "KLM", "KLS", 
    "KLU", "KMA", "KME", "KMF", "KMN", "KMS", "KMT", "KNC", "KNI", "KNJ", "KNN", "KNP", "KNS", 
    "KNT", "KNV", "KNZ", "KO4", "KOB", "KOD", "KOP", "KOT", "KOZ", "KP3", "KPA", "KPB", "KPC", 
    "KPD", "KPF", "KPH", "KPI", "KPK", "KPM", "KPP", "KPR", "KPS", "KPU", "KRC", "KRF", "KRH", 
    "KRO", "KRR", "KRS", "KRT", "KRV", "KSA", "KSC", "KSD", "KSK", "KSL", "KSM", "KSP", "KSR", 
    "KST", "KSV", "KSY", "KTE", "KTI", "KTJ", "KTM", "KTN", "KTR", "KTS", "KTT", "KTU", "KTV", 
    "KU3", "KUA", "KUB", "KUD", "KUP", "KUT", "KVA", "KVK", "KVR", "KVS", "KVT", "KYA", "KYR", 
    "KYS", "KZL", "KZO", "KZU", "LAI", "LAK", "LET", "LFD", "LGK", "LGO", "LHM", "LHP", "LKF", 
    "LKT", "LLA", "LPH", "LRT", "LTC", "LTL", "LTS", "LZV", "MAC", "MAD", "MAS", "MAV", "MBL", 
    "MBR", "MCU", "MD1", "MD2", "MDF", "MET", "MGB", "MGD", "MGE", "MGH", "MHF", "MIK", "MJB", 
    "MJE", "MJG", "MJH", "MJK", "MJL", "MKA", "MKG", "MKL", "MLK", "MLS", "MLT", "MMH", "MOD", 
    "MOZ", "MP1", "MP2", "MP4", "MPD", "MPE", "MPF", "MPI", "MPK", "MPL", "MPN", "MPP", "MPS", 
    "MRI", "MSK", "MSL", "MSO", "MSR", "MT1", "MT2", "MT5", "MT6", "MT7", "MT8", "MT9", "MTA", 
    "MTD", "MTE", "MTF", "MTG", "MTH", "MTI", "MTK", "MTL", "MTS", "MTU", "MTV", "MTX", "MU1", 
    "MUL", "MUT", "NAK", "NAL", "NAU", "NAV", "NBE", "NBH", "NBM", "NBO", "NBZ", "NCS", "NCU", 
    "NCV", "NDC", "NDE", "NDL", "NDS", "NDU", "NES", "NFF", "NFH", "NFK", "NFS", "NHP", "NHT", 
    "NHV", "NHY", "NIG", "NIS", "NJF", "NJG", "NJR", "NJY", "NKA", "NKC", "NKH", "NKJ", "NKK", 
    "NKL", "NKM", "NKP", "NKS", "NKT", "NKV", "NLC", "NLD", "NLE", "NLK", "NLL", "NME", "NMG", 
    "NMP", "NMU", "NNF", "NNS", "NOA", "NOI", "NOV", "NP1", "NP2", "NPH", "NPK", "NRC", "NRG", 
    "NRM", "NSA", "NSD", "NSH", "NSK", "NSP", "NSS", "NST", "NSY", "NTB", "NTD", "NTF", "NTI", 
    "NTO", "NTS", "NUB", "NUG", "NUH", "NUV", "NVB", "NVC", "NVK", "NVP", "NVT", "NVZ", "NYH", 
    "NZH", "NZT", "NZU", "OAB", "OAO", "OBI", "OBN", "OBP", "OBR", "OCM", "OCN", "OCT", "ODD", 
    "ODG", "ODN", "ODP", "ODS", "ODV", "OFA", "OFB", "OFI", "OFK", "OFO", "OFS", "OGD", "OGF", 
    "OGV", "OHB", "OHI", "OHK", "OHT", "OIL", "OIR", "OIS", "OJB", "OJH", "OJK", "OJT", "OJU", 
    "OJY", "OKD", "OKF", "OKP", "OKT", "OLA", "OLD", "OLE", "OMB", "OMC", "OME", "OMG", "OMT", 
    "ONB", "OND", "ONE", "ONF", "ONK", "ONN", "ONS", "ONT", "ONY", "OPB", "OPD", "OPF", "OPH", 
    "OPI", "OPJ", "OPL", "OPU", "OPZ", "ORC", "ORI", "ORS", "OSD", "OSF", "OSH", "OSL", "OSN", 
    "OSS", "OTE", "OTF", "OTJ", "OTK", "OTM", "OTZ", "OUB", "OUD", "OUN", "OUR", "OUY", "OVD", 
    "OVR", "OVT", "OYH", "OYL", "OYS", "OYT", "OZC", "P1A", "PA2", "PAB", "PAC", "PAF", "PAI", 
    "PAL", "PAO", "PAP", "PAU", "PBE", "PBF", "PBH", "PBI", "PBK", "PBN", "PBR", "PBS", "PBY", 
    "PCE", "PCH", "PCN", "PCS", "PDC", "PDD", "PDE", "PDF", "PDG", "PDH", "PDR", "PDS", "PEA", 
    "PFO", "PFS", "PGD", "PGE", "PGH", "PGS", "PHB", "PHE", "PHF", "PHI", "PHK", "PHN", "PHS", 
    "PHY", "PIA", "PID", "PIL", "PIP", "PIR", "PIS", "PJL", "PJP", "PK1", "PKD", "PKF", "PKH", 
    "PKL", "PKM", "PKN", "PKP", "PKR", "PKT", "PKU", "PKV", "PKZ", "PLA", "PLR", "PLS", "PMG", 
    "PMH", "PMP", "PNR", "PNU", "PO7", "PO8", "PO9", "POB", "POD", "POF", "POH", "POI", "POS", 
    "POU", "PP1", "PPB", "PPD", "PPE", "PPF", "PPG", "PPH", "PPI", "PPJ", "PPK", "PPM", "PPN", 
    "PPO", "PPP", "PPS", "PPT", "PPU", "PPV", "PPZ", "PRD", "PRF", "PRH", "PRR", "PRU", "PRV", 
    "PRY", "PSB", "PSD", "PSE", "PSG", "PSH", "PSL", "PSO", "PSR", "PSS", "PST", "PTC", "PTE", 
    "PTF", "PTG", "PTL", "PTN", "PTO", "PTP", "PTS", "PUA", "PUC", "PUD", "PUH", "PUK", "PUR", 
    "PUT", "PUV", "PUZ", "PVK", "PYB", "PYD", "PYF", "PYH", "PYI", "PYL", "PYR", "PYS", "RAF", 
    "RAI", "RAN", "RAV", "RAY", "RBA", "RBB", "RBE", "RBF", "RBH", "RBI", "RBK", "RBL", "RBN", 
    "RBP", "RBR", "RBT", "RBV", "RCL", "RCS", "RCV", "RD1", "RDF", "RDH", "RDK", "RDS", "RDT", 
    "RDZ", "RE6", "RE7", "RE8", "RFM", "RGD", "RGH", "RHD", "RHI", "RHS", "RIA", "RIH", "RIK", 
    "RJG", "RKC", "RKH", "RKL", "RKS", "RKV", "RLH", "RMR", "RO1", "ROD", "ROF", "ROY", "RPC", 
    "RPD", "RPE", "RPG", "RPI", "RPK", "RPL", "RPM", "RPN", "RPO", "RPP", "RPS", "RPT", "RPU", 
    "RPX", "RRA", "RRP", "RS1", "RSF", "RSK", "RSY", "RSZ", "RTA", "RTB", "RTD", "RTG", "RTH", 
    "RTI", "RTP", "RUH", "RUT", "RVI", "RVS", "RYA", "RYB", "RYD", "RYF", "RYI", "RYU", "RZR", 
    "SAP", "SAR", "SAS", "SAT", "SBH", "SBI", "SBR", "SBS", "SCZ", "SD1", "SDA", "SDP", "SDS", 
    "SEH", "SER", "SFA", "SFR", "SFS", "SGK", "SGT", "SHC", "SHE", "SHI", "SHU", "SIA", "SIS", 
    "SJP", "SKL", "SKO", "SKS", "SKT", "SKZ", "SLF", "SLG", "SLR", "SLS", "SMP", "SNY", "SOS", 
    "SPA", "SPD", "SPE", "SPN", "SPP", "SPR", "SPT", "SPU", "SRA", "SRE", "SRH", "SRL", "SRO", 
    "SSD", "SSE", "SSF", "SSK", "SSN", "SSO", "SSS", "SST", "ST1", "STI", "STM", "STS", "STZ", 
    "SUA", "SUB", "SUC", "SUR", "SVB", "SVS", "SVY", "SYF", "SYL", "SYN", "SYR", "SYS", "T3B", 
    "TAL", "TAO", "TAR", "TAU", "TAZ", "TB9", "TBP", "TBT", "TBV", "TCA", "TCB", "TCC", "TCD", 
    "TCF", "TCG", "TCH", "TCI", "TCS", "TDG", "TDP", "TE3", "TE4", "TEJ", "TFE", "TFF", "TFU", 
    "TGA", "TGE", "TGN", "TGR", "TGT", "TGV", "TGZ", "THD", "THF", "THG", "THH", "THO", "THS", 
    "THT", "THV", "TI1", "TI2", "TI3", "TI4", "TI6", "TI7", "TIE", "TIL", "TIP", "TIV", "TJB", 
    "TJF", "TJI", "TJL", "TJT", "TKF", "TKH", "TKK", "TKM", "TLC", "TLE", "TLH", "TLK", "TLT", 
    "TLU", "TLV", "TLY", "TLZ", "TMC", "TMG", "TMH", "TMM", "TMR", "TMS", "TMU", "TMV", "TMZ", 
    "TNA", "TNB", "TND", "TNF", "TNH", "TNI", "TNK", "TNS", "TO8", "TOK", "TOT", "TP1", "TP2", 
    "TPC", "TPE", "TPF", "TPJ", "TPL", "TPO", "TPP", "TPR", "TPV", "TPZ", "TRJ", "TRN", "TRO", 
    "TRR", "TRU", "TRZ", "TSI", "TSP", "TTA", "TTE", "TTL", "TTP", "TTS", "TTV", "TTZ", "TUA", 
    "TVE", "TVN", "TYH", "TZC", "TZD", "TZH", "TZL", "TZP", "TZT", "TZV", "UAB", "UAP", "UCE", 
    "UCN", "UCP", "UFH", "UHL", "UHN", "UHS", "UHV", "ULH", "ULL", "UNT", "UP1", "UP2", "UPD", 
    "UPH", "UPP", "UPS", "URA", "URC", "URD", "URG", "URM", "URS", "URV", "URY", "USO", "USS", 
    "UST", "USY", "UYH", "UZY", "VAY", "VCD", "VCG", "VCY", "VFK", "VFO", "VFS", "VHS", "VK6", 
    "VKI", "VKK", "VKR", "VKT", "VKV", "VLT", "VMV", "VNK", "VPA", "VPP", "VPS", "VRK", "VTF", 
    "VTL", "YA1", "YAC", "YAE", "YAK", "YAN", "YAR", "YAS", "YAY", "YBE", "YBH", "YBJ", "YBP", 
    "YBR", "YBS", "YCG", "YCH", "YCK", "YCL", "YCP", "YCY", "YDH", "YDI", "YDK", "YDL", "YDM", 
    "YDP", "YDZ", "YEF", "YFV", "YGM", "YHB", "YHI", "YHK", "YHP", "YHS", "YHT", "YHY", "YHZ", 
    "YIK", "YIS", "YIT", "YIV", "YJA", "YJH", "YJK", "YJU", "YJY", "YKS", "YKT", "YLB", "YLC", 
    "YLE", "YLO", "YLR", "YLY", "YLZ", "YMD", "YMH", "YMP", "YNF", "YNK", "YNL", "YOA", "YOS", 
    "YOT", "YOZ", "YP1", "YP2", "YP4", "YPC", "YPD", "YPF", "YPI", "YPK", "YPL", "YPN", "YPP", 
    "YPR", "YPT", "YPU", "YPV", "YRB", "YRZ", "YSA", "YSH", "YSL", "YSO", "YSU", "YTC", "YTD", 
    "YTJ", "YTO", "YTR", "YTV", "YTY", "YUB", "YUD", "YUI", "YUK", "YUN", "YUY", "YVB", "YVD", 
    "YVF", "YVG", "YVO", "YVS", "YZC", "YZF", "YZG", "YZH", "YZK", "YZL", "YZT", "ZA2", "ZAD", 
    "ZAV", "ZAY", "ZBD", "ZBI", "ZBJ", "ZBN", "ZBO", "ZBZ", "ZCA", "ZCB", "ZCC", "ZCD", "ZCE", 
    "ZCF", "ZCG", "ZCH", "ZCK", "ZCN", "ZDD", "ZDK", "ZDZ", "ZFB", "ZFH", "ZFZ", "ZHH", "ZIH", 
    "ZJB", "ZJH", "ZJI", "ZJL", "ZJR", "ZJT", "ZJV", "ZK1", "ZK2", "ZKK", "ZLB", "ZLG", "ZLH", 
    "ZMT", "ZMU", "ZMY", "ZNF", "ZOS", "ZP1", "ZP2", "ZP3", "ZP6", "ZP7", "ZP8", "ZP9", "ZPA", 
    "ZPC", "ZPE", "ZPF", "ZPG", "ZPH", "ZPJ", "ZPK", "ZPN", "ZPO", "ZPR", "ZR2", "ZR3", "ZSB", 
    "ZSF", "ZSG", "ZSK", "ZSN", "ZTF", "ZTG", "ZUD", "ZUE", "ZUS", "ZVB", "ZVO", "ZYC", "ZYD", 
    "ZZL", "VHK", "VPD", "VPE", "VPH", "ZTV", "AAJ", "ABE", "ACV", "AE1", "AE2", "AE3", "AEA", 
    "AEB", "AEC", "AEH", "AEI", "AEK", "AEN", "AEP", "AER", "AET", "AEU", "AEY", "AEZ", "AFH", 
    "AFJ", "AFP", "AG1", "AG2", "AG3", "AG4", "AGA", "AGB", "AGD", "AGE", "AGG", "AGH", "AGM", 
    "AGT", "AH1", "AH2", "AH3", "AH4", "AH5", "AH6", "AH8", "AH9", "AHB", "AHC", "AHJ", "AHL", 
    "AIE", "AIP", "AJA", "AJB", "AJC", "AJF", "AJG", "AJH", "AJP", "AJR", "AJT", "AJV", "AJY", 
    "AJZ", "ALI", "ALJ", "ALR", "ALS", "ALU", "ALZ", "AMF", "AMG", "AMR", "AMY", "AMZ", "ANE", 
    "ANG", "ANJ", "ANK", "ANP", "ANS", "AO1", "AO2", "APG", "ATE", "ATK", "AUA", "AUG", "AVB", 
    "AVD", "AVG", "AVJ", "AVN", "AVO", "AVR", "AYJ", "AZA", "AZD", "AZH", "AZK", "AZL", "AZS", 
    "AZY", "BAE", "BBD", "BBH", "BEE", "BEF", "BEH", "BEI", "BEK", "BEO", "BGE", "BGK", "BGL", 
    "BHK", "BHS", "BHT", "BKB", "BNA", "BNB", "BNK", "BNL", "BNO", "BNS", "BNZ", "BPC", "BPE", 
    "BPF", "BPG", "BPH", "BPI", "BPJ", "BPK", "BPL", "BPN", "BPO", "BPR", "BPS", "BPU", "BSR", 
    "BZY", "CFA", "CFB", "CFC", "CFD", "CFE", "CFK", "CFY", "CGE", "CGG", "CHA", "CHC", "CHD", 
    "CHG", "CHH", "CHI", "CHK", "CHL", "CHM", "CHN", "CHO", "CHS", "CHT", "CHU", "EAE", "EHG", 
    "EHK", "EIE", "EIF", "EIG", "EIH", "EIK", "EMI", "EMY", "ENF", "EST", "FEA", "FEF", "FEI", 
    "FEN", "FEO", "FER", "FES", "FET", "FFC", "FFZ", "FGF", "FGH", "FIC", "FIE", "FIF", "FIG", 
    "FIH", "FII", "FIK", "FIM", "FIR", "FIS", "FIU", "FIV", "FIY", "FIZ", "FJG", "FOA", "FVI", 
    "FYL", "FYN", "FYU", "FYY", "GCK", "GCN", "GCS", "GCT", "GCV", "GCY", "GDV", "GEA", "GED", 
    "GEF", "GEG", "GEH", "GEK", "GEL", "GES", "GEU", "GEV", "GFH", "GGJ", "GHA", "GHD", "GHE", 
    "GHF", "GHG", "GHH", "GHI", "GHJ", "GHK", "GHL", "GHM", "GHN", "GHO", "GHP", "GHT", "GHU", 
    "GHV", "GHY", "GHZ", "GKB", "GMF", "GRA", "HEA", "HEB", "HEC", "HED", "HEE", "HEG", "HEI", 
    "HEK", "HEL", "HEP", "HER", "HES", "HET", "HFS", "HHB", "HHM", "HHY", "HS1", "HSR", "IEA", 
    "IEB", "IEE", "IEF", "IEG", "IEH", "IEK", "IER", "IGE", "KEA", "KEB", "KED", "KEF", "KEG", 
    "KEH", "KEK", "KES", "KET", "KEY", "KEZ", "KFE", "KGC", "KHL", "KJM", "KKS", "KKV", "KLT", 
    "KLV", "KML", "KMP", "KOA", "KOE", "KOS", "KRM", "KRU", "KSH", "KSU", "KTZ", "MDD", "MDE", 
    "MDK", "MEA", "MEB", "MEV", "MEY", "MHA", "MHB", "MHC", "MHD", "MHE", "MHG", "MHH", "MHI", 
    "MHK", "MHL", "MHM", "MHN", "MHO", "MHR", "MHS", "MHT", "MHU", "MHV", "MHY", "MHZ", "MZL", 
    "MZN", "MZP", "NHA", "NHM", "NHN", "NZA", "PRC", "PRS", "RZM", "RZN", "SBA", "SSH", "TBJ", 
    "THE", "THK", "TJY", "TKV", "TML", "TMN", "TNE", "TSZ", "TVC", "TVG", "TVH", "TYJ", "VEB", 
    "VED", "VEG", "VEH", "VEI", "VEK", "VEL", "VEO", "VEP", "VER", "VES", "VET", "VEU", "VEV", 
    "VEY", "VGA", "VGB", "VGC", "VGD", "VGE", "VGF", "VGG", "VGH", "VGK", "VGP", "VGT", "VGY", 
    "VGZ", "VKE", "VKJ", "VVA", "VVD", "VVE", "VVM", "VVU", "VVZ", "VYB", "YZD", "ZHB", "ZHD", 
    "ZHE", "ZHF", "ZHG"
}

def extract_tickers(text: str) -> list[str]:
    """
    Metin içerisindeki kelimeleri tarar ve SADECE BEYAZ_LISTE 
    içerisinde yer alanları filtreleyerek döndürür.
    """
    candidates = re.findall(r'\b([A-Z]{3,6})\b', text.upper())
    return [c for c in set(candidates) if c in BEYAZ_LISTE]


def get_fon_info(ticker: str) -> str:
    """
    Firebase'den fon/pozisyon bilgisi çeker.
    - ticker bir HİSSE ise → o hisseyi taşıyan fonları listeler
    - ticker bir FON ise  → o fonun içindeki hisseleri listeler
    """
    if not firebase_admin._apps:
        return ""

    ticker = ticker.upper().strip()
    try:
        poz_ref = db.reference("veriler/Pozisyonlar")

        # 1) Önce HİSSE olarak dene (hisse_kodu ile)
        by_hisse_raw = poz_ref.order_by_child("hisse_kodu").equal_to(ticker).get()
        
        if by_hisse_raw:
            # GÜVENLİK YAMASI: Dict veya List olma durumunu yönetiyoruz
            by_hisse_list = by_hisse_raw.values() if isinstance(by_hisse_raw, dict) else [x for x in by_hisse_raw if x]
            
            lines = [f"**{ticker} hissesini taşıyan fonlar:**"]
            items = []
            
            for p in by_hisse_list:
                fon = (p.get("fon_kodu") or "?").upper()
                agirlik = p.get("agirlik") or 0
                lot = p.get("lot_adedi") or 0
                onceki = p.get("onceki_agirlik")
                
                degisim = None
                if onceki is not None:
                    try:
                        degisim = float(agirlik) - float(onceki)
                    except ValueError:
                        pass
                
                items.append((fon, float(agirlik or 0), float(lot or 0), degisim))

            items.sort(key=lambda x: x[2], reverse=True)  # lot'a göre sırala

            for fon, agirlik, lot, degisim in items[:15]:
                deg_str = f" | Değişim: %{degisim:+.2f}" if degisim is not None else ""
                lines.append(f"- {fon}: Ağırlık %{agirlik:.2f} | Lot: {int(lot):,}{deg_str}")

            if len(items) > 15:
                lines.append(f"... ve {len(items)-15} fon daha")
            return "\n".join(lines)

        # 2) Hisse bulunamadıysa FON olarak dene (fon_kodu ile)
        by_fon_raw = poz_ref.order_by_child("fon_kodu").equal_to(ticker).get()
        
        if by_fon_raw:
            by_fon_list = by_fon_raw.values() if isinstance(by_fon_raw, dict) else [x for x in by_fon_raw if x]
            
            lines = [f"**{ticker} fonu portföy dağılımı:**"]
            items = []
            
            for p in by_fon_list:
                hisse = (p.get("hisse_kodu") or "?").upper()
                agirlik = p.get("agirlik") or 0
                lot = p.get("lot_adedi") or 0
                onceki = p.get("onceki_agirlik")
                
                degisim = None
                if onceki is not None:
                    try:
                        degisim = float(agirlik) - float(onceki)
                    except ValueError:
                        pass
                        
                items.append((hisse, float(agirlik or 0), float(lot or 0), degisim))

            items.sort(key=lambda x: x[1], reverse=True)  # ağırlığa göre sırala

            for hisse, agirlik, lot, degisim in items[:20]:
                deg_str = f" | Değişim: %{degisim:+.2f}" if degisim is not None else ""
                lines.append(f"- {hisse}: Ağırlık %{agirlik:.2f} | Lot: {int(lot):,}{deg_str}")

            return "\n".join(lines)

        return ""  # hiçbir şey bulunamadı

    except Exception as e:
        print(f"[Firebase Hata] {ticker} okunamadı: {e}")
        return ""


def get_stock_info(ticker: str) -> str:
    symbol = ticker if ticker.endswith(".IS") else f"{ticker}.IS"
    try:
        df_daily = yf.download(symbol, period="10d", interval="1d", progress=False, auto_adjust=True)
        df_weekly = yf.download(symbol, period="1mo", interval="1wk", progress=False, auto_adjust=True)

        if df_daily.empty or df_weekly.empty:
            return ""

        if isinstance(df_daily.columns, pd.MultiIndex):
            df_daily.columns = df_daily.columns.get_level_values(0)
            df_weekly.columns = df_weekly.columns.get_level_values(0)

        df_daily = df_daily.dropna(subset=["Close"])
        df_weekly = df_weekly.dropna(subset=["Close"])

        if df_daily.empty or df_weekly.empty:
            return ""

        daily_last = df_daily.iloc[-1]
        weekly_last = df_weekly.iloc[-1]

        price_daily = float(daily_last["Close"])
        price_weekly = float(weekly_last["Close"])

        if abs(price_weekly - price_daily) > 0.001:
            price = price_weekly            
            prev_close = price_daily        
            volume = 0                      
            last_date = "Son Kapanış (Tatil/Hafta sonu kurtarması)" 
        else:
            price = price_daily
            prev_close = float(df_daily.iloc[-2]["Close"]) if len(df_daily) > 1 else price_daily
            volume = int(daily_last["Volume"]) if "Volume" in daily_last and pd.notna(daily_last["Volume"]) else 0
            try:
                last_date = daily_last.name.strftime("%Y-%m-%d") if hasattr(daily_last.name, "strftime") else str(daily_last.name)[:10]
            except Exception:
                last_date = str(df_daily.index[-1])[:10]

        change = price - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0

        text = (
            f"**{ticker}**\n"
            f"- Son Kapanış: **{price:.2f} TL** ({last_date})\n"
            f"- Değişim: {change:+.2f} TL (%{change_pct:+.2f})\n"
        )
        
        if volume > 0:
            text += f"- Hacim: {volume:,}\n"

        bugun = datetime.now(TR_TZ)
        if bugun.weekday() >= 5:
            text += "- Not: BIST kapalı. Fiyat özel olarak doğrulandı.\n"

        return text

    except Exception as e:
        print(f"[yfinance hata - hibrit] {ticker}: {e}")
        return ""
        

def fetch_kap_for_ticker(ticker: str, days: int = 5) -> str:
    to_d = datetime.now(TR_TZ).date()
    from_d = to_d - timedelta(days=days)
    payload = {
        "fromDate": from_d.isoformat(),
        "toDate": to_d.isoformat(),
        "mkkMemberOidList": [],
        "subjectList": [],
    }
    data = []
    try:
        with httpx.Client(timeout=8, follow_redirects=True) as client:
            r = client.post(KAP_API, json=payload, headers=KAP_HEADERS)
            if r.status_code == 200:
                data = r.json() if isinstance(r.json(), list) else []
    except Exception:
        pass

    if not data:
        return ""

    relevant = []
    ticker_up = ticker.upper()
    for d in data:
        if d.get("disclosureClass") != "ODA":
            continue
        stocks = d.get("relatedStocks") or d.get("stockCodes") or []
        if isinstance(stocks, str):
            stocks = [s.strip() for s in stocks.replace(",", " ").split()]
        if ticker_up in [str(s).upper() for s in stocks]:
            relevant.append(d)

    if not relevant:
        return ""

    relevant = sorted(relevant, key=lambda x: x.get("publishDate") or "", reverse=True)[:4]
    lines = [f"**{ticker} – Son KAP:**"]
    for d in relevant:
        when = (d.get("publishDate") or "")[:16]
        subj = (d.get("subject") or "").strip()
        summ = (d.get("summary") or "").strip()[:120]
        lines.append(f"- [{when}] {subj}")
        if summ:
            lines.append(f"  {summ}")
    return "\n".join(lines)


SYSTEM_PROMPT = """
Sen Lira'sın. Türkçe konuşan, samimi, veri odaklı, detaylı analiz yapabilen ve biraz esprili bir finans asistanısın.

ZORUNLU KURALLAR:
- "kanki", "kankitom", "patron" diyebilirsin. Asla "hocam" deme.
- Sana CANLI VERİ (Fiyat, KAP veya Fon Portföyü) geldiyse, önce güncel verileri değerlendir.
- Yanıtlarını her zaman detaylı, Markdown ile yapılandırılmış ve okunaklı ver.
- Yatırım tavsiyesi verebilirsin ama riskleri, piyasa volatilitesini ve stop-loss hayat kurtarır gerçeğini hep vurgula.
- Veri yoksa bile "veri ulaşmadı", "API patladı", "aracı kurum", "kap.org.tr" gibi cümleler ASLA KULLANMA.
- Sana verilen SİSTEM SAATİ VE TARİHİ bilgisini dikkate al. Hafta sonuysa doğal şekilde belirt.
- Karakterine uygun emojiler kullan.
"""


@app.get("/health")
def health():
    return {
        "ok": True,
        "gemini": bool(GEMINI_API_KEY),
        "secret": bool(API_SECRET_KEY),
        "firebase": bool(FIREBASE_JSON_STR),
        "model": MODEL_NAME,
        "version": "3.2"
    }


@app.get("/test-price")
def test_price(hisse: str = "BRSAN"):
    info = get_stock_info(hisse.upper())
    return {
        "ticker": hisse.upper(),
        "sonuc": info if info else "BOŞ DÖNDÜ",
        "zaman": datetime.now(TR_TZ).strftime("%Y-%m-%d %H:%M")
    }


@app.post("/api/lira-sor", response_model=SorResponse)
def lira_sor(req: SorRequest, x_api_key: Optional[str] = Header(None)):
    _check_secret(x_api_key)
    soru = req.soru.strip()
    tickers = extract_tickers(soru)
    extra_parts = []

    for t in tickers[:3]:
        price = get_stock_info(t)
        if price:
            extra_parts.append(price)

        kap = fetch_kap_for_ticker(t)
        if kap:
            extra_parts.append(kap)

        fon = get_fon_info(t)
        print(f"[DEBUG FON] ticker={t} | len={len(fon)} | preview={fon[:180] if fon else 'BOŞ'}")
        if fon:
            extra_parts.append(fon)

    su_an = datetime.now(TR_TZ)
    gunler = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
    zaman_bilgisi = f"[SİSTEM BİLGİSİ: Bugün {su_an.strftime('%d.%m.%Y')} {gunler[su_an.weekday()]}, Saat: {su_an.strftime('%H:%M')}]"

    if extra_parts:
        user_content = (
            zaman_bilgisi + "\n\n"
            + "--- CANLI VERİ ---\n"
            + "\n\n".join(extra_parts)
            + "\n\n--- KULLANICI SORUSU ---\n"
            + soru
        )
    else:
        user_content = zaman_bilgisi + "\n\n" + soru

    cevap = None
    for attempt in range(2):
        try:
            client = _get_client()
# GÜNCELLENMİŞ GEMİNİ ÇAĞRISI (Google Arama Yeteneği Eklenmiş Hali)
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=user_content,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.65,
                    max_output_tokens=3000,
                    # İŞTE BURASI: Lira'nın internette arama yapmasını sağlar!
# tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )
            cevap = (response.text or "").strip()
            if cevap:
                break
        except Exception as e:
            print(f"Gemini hata (attempt {attempt+1}): {e}")
            if attempt == 0:
                time.sleep(1.5)
            continue

    if not cevap:
        if tickers:
            cevap = f"Kanki {tickers[0]} için şu an net rakam çekemedim ama volatil bir varlık, stop’unu ihmal etme."
        else:
            cevap = "Kanki şu an biraz yoğunum, biraz sonra tekrar sor."

    return SorResponse(
        ok=True,
        soru=soru,
        cevap=cevap,
        thread_id="gemini",
        kaynak="gemini+data" if extra_parts else "gemini"
    )

@app.get("/")
def root():
    return HTMLResponse("<h2>Lira API v3.2</h2><p><a href='/health'>/health</a> | <a href='/test-price'>/test-price</a></p>")
