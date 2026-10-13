#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PicasaExport v1.1.0
===================
Marque       : CGExcel
Auteur       : Cyrille Gindre
Copyright    : © 2026 Cyrille Gindre — CGExcel
Licence      : MIT + « BAL 1.0 — Bonne Action License » (voir le fichier LICENSE)
               En échange, une seule chose vous est demandée, sur l'honneur : faire une bonne
               action chaque jour. Aider un voisin, sourire à un inconnu, ramasser un papier…
               c'est vous qui voyez.

But : extraire d'une photothèque Picasa (retouches stockées dans les fichiers
      .picasa.ini, invisibles hors de Picasa) une arborescence « normale » :
        - mêmes dossiers que la source ;
        - photos retouchées (rendu des retouches) à leur place habituelle ;
        - originaux dans un sous-dossier (« _Originaux_Picasa ») ;
        - dates et métadonnées (EXIF…) conservées ;
        - catalogue CSV : personnes reconnues, favoris, retouches.

Dépendances : Python 3.8+, Pillow, numpy   (pip install pillow numpy)
Optionnel   : exiftool dans le PATH, pour écrire personnes/favoris DANS les photos.
"""

import argparse
import hashlib
import io
import struct
import json
from collections import deque
import csv
import html
import math
import ctypes
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

VERSION = "1.1.0"

PHRASE_BAL = ("En échange, une seule chose vous est demandée, sur l'honneur : faire une bonne "
              "action chaque jour. Aider un voisin, sourire à un inconnu, ramasser un papier… "
              "c'est vous qui voyez.")

APROPOS = (
    f"PicasaExport v{VERSION} — CGExcel — © 2026 Cyrille Gindre\n"
    f"Licence MIT + BAL 1.0 (Bonne Action License).\n{PHRASE_BAL}"
)

EXT_IMAGES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
FICHIERS_IGNORES = {".picasa.ini", "picasa.ini", "thumbs.db", "desktop.ini", ".picasaoriginals"}
DOSSIERS_IGNORES = {".picasaoriginals"}
ID_INCONNU = "ffffffffffffffff"

# OpenBLAS (numpy) réserve de la mémoire pour chaque cœur : sur un vieux PC ou un Python 32 bits,
# cela provoque « Insufficient memory » / « OpenBLAS error: Memory allocation still failed ».
# Un seul fil de calcul suffit largement ici ; à fixer AVANT l'import de numpy.
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

try:
    from PIL import Image, ImageFile, ImageFilter, ImageOps
    import numpy as np
except ImportError:  # pragma: no cover
    print("Il manque Pillow et/ou numpy. Installez-les avec :  pip install pillow numpy")
    sys.exit(1)

ImageFile.LOAD_TRUNCATED_IMAGES = True   # photos tronquées : on récupère ce qui peut l'être
Image.MAX_IMAGE_PIXELS = None            # grands scans : pas d'avertissement « decompression bomb »


# --------------------------------------------------------------------------- lecture des .ini

def lire_texte(chemin):
    with open(chemin, "rb") as f:
        brut = f.read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return brut.decode(enc)
        except UnicodeDecodeError:
            continue
    return brut.decode("utf-8", errors="replace")


def lire_ini(dossier):
    """Retourne (sections, contacts) pour un dossier.
    sections : {nom_fichier_minuscule: {clé_minuscule: valeur}}
    contacts : {id: nom}
    """
    sections, contacts, liens = {}, {}, {}
    for nom in (".picasa.ini", "Picasa.ini", "picasa.ini"):
        chemin = os.path.join(dossier, nom)
        if not os.path.isfile(chemin):
            continue
        courant = None
        for ligne in lire_texte(chemin).splitlines():
            ligne = ligne.strip()
            if not ligne or ligne.startswith(";"):
                continue
            m = re.match(r"^\[(.+)\]$", ligne)
            if m:
                courant = m.group(1)
                if courant.lower() not in ("contacts", "contacts2", "picasa"):
                    sections.setdefault(courant.lower(), {})
                continue
            if courant is None or "=" not in ligne:
                continue
            cle, _, val = ligne.partition("=")
            cle = cle.strip()
            if courant.lower() in ("contacts", "contacts2"):
                nom_contact = val.split(";")[0].strip()
                # [Contacts] (ancien format) ne contient souvent que des liens vers un compte Google
                # (ex. « mon.compte@xxx_lh,219eb9ec0913fe5a ») : ce n'est pas un nom. On retient
                # l'identifiant Google (après la virgule), pour retrouver le nom dans contacts.xml.
                if re.search(r"_lh,", val):
                    liens.setdefault(cle.lower(), []).extend(
                        x.lower() for x in re.findall(r"_lh,([0-9a-fA-F]+)", val))
                    continue
                if re.search(r"@", nom_contact) or not nom_contact:
                    continue
                contacts[cle.lower()] = nom_contact
            elif courant.lower() != "picasa":
                sections[courant.lower()][cle.lower()] = val.strip()
        # on lit le premier fichier trouvé
        break
    return sections, contacts, liens


def contacts_globaux(chemin_xml=None):
    """Lit contacts.xml de Picasa (noms des personnes) si disponible."""
    candidats = []
    if chemin_xml:
        candidats.append(chemin_xml)
    for var in ("LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(var)
        if base:
            candidats.append(os.path.join(base, "Google", "Picasa2", "contacts", "contacts.xml"))
            candidats.append(os.path.join(base, "Google", "Picasa3", "contacts", "contacts.xml"))
    par_id, par_focus = {}, {}
    for c in candidats:
        if os.path.isfile(c):
            texte = lire_texte(c)
            # <contact id=".." name=".."> [<subject id=".." focus_ids=".."/> ...] </contact>
            for m in re.finditer(r'<contact\b([^>]*?)(?:/>|>(.*?)</contact>)', texte, re.S):
                attrs = dict(re.findall(r'([\w:.-]+)="([^"]*)"', m.group(1)))
                nom = html.unescape(attrs.get("name", "")).strip()
                if not nom:
                    continue
                if attrs.get("id"):
                    par_id[attrs["id"].lower()] = nom
                for sujet in re.finditer(r'<subject\b([^>]*)', m.group(2) or ""):
                    a = dict(re.findall(r'([\w:.-]+)="([^"]*)"', sujet.group(1)))
                    for cle in ("id", "focus_ids"):
                        for v in re.split(r"[,;\s]+", a.get(cle, "")):
                            if v:
                                par_focus.setdefault(v.lower(), nom)
            break
    return par_id, par_focus


def noms_du_dossier(contacts_loc, liens, glob_id, glob_focus):
    """Fusionne les sources de noms : liens Google (.picasa.ini -> contacts.xml), contacts.xml par
    identifiant, puis [Contacts2] du dossier (prioritaire)."""
    noms = {}
    for cid, focus in liens.items():
        for f in focus:
            if f in glob_focus:
                noms[cid] = glob_focus[f]
                break
    noms.update(glob_id)
    noms.update(contacts_loc)
    return noms


def parser_filtres(valeur):
    """'crop64=1,3fff...;fill=1,0.3;' -> [('crop64', ['1','3fff...']), ('fill', ['1','0.3'])]"""
    res = []
    for bloc in (valeur or "").split(";"):
        bloc = bloc.strip()
        if not bloc or "=" not in bloc:
            continue
        nom, _, reste = bloc.partition("=")
        res.append((nom.strip().lower(), [a.strip() for a in reste.split(",")]))
    return res


def parser_rotation(valeur):
    m = re.search(r"rotate\((\d+)\)", valeur or "", re.I)
    return int(m.group(1)) % 4 if m else 0


def parser_visages(valeur, contacts):
    noms = []
    for m in re.finditer(r"rect64\(([0-9a-fA-F]+)\)\s*,\s*([0-9a-fA-F]+)", valeur or ""):
        cid = m.group(2).lower()
        if cid == ID_INCONNU:
            continue
        nom = contacts.get(cid)
        if nom and nom not in noms:
            noms.append(nom)
    return noms


def rectangles_visages(valeur):
    """'rect64(hex),id;...' -> liste de (gauche, haut, droite, bas) en fractions du fichier brut."""
    res = []
    for m in re.finditer(r"rect64\(([0-9a-fA-F]+)\)", valeur or ""):
        h = m.group(1).rjust(16, "0")
        if len(h) != 16 or h == "f" * 16:
            continue
        g, t, d, b = (int(h[i:i + 4], 16) / 65535.0 for i in (0, 4, 8, 12))
        if d > g and b > t:
            res.append((g, t, d, b))
    return res


def visages_detail(valeur, contacts):
    """-> liste de (nom ou None, (g, t, d, b)) pour chaque visage de la photo (None = non identifié)."""
    res = []
    for m in re.finditer(r"rect64\(([0-9a-fA-F]+)\)\s*,\s*([0-9a-fA-F]+)", valeur or ""):
        h = m.group(1).rjust(16, "0")
        if len(h) != 16:
            continue
        g, t, d, b = (int(h[i:i + 4], 16) / 65535.0 for i in (0, 4, 8, 12))
        if d <= g or b <= t:
            continue
        cid = m.group(2).lower()
        res.append((None if cid == ID_INCONNU else contacts.get(cid), (g, t, d, b)))
    return res


def _masque_rect(taille, rect, cote=700):
    """Image-témoin à l'échelle réduite : un rectangle blanc sur fond noir."""
    w, h = taille
    k = cote / max(w, h)
    pw, ph = max(int(round(w * k)), 2), max(int(round(h * k)), 2)
    im = Image.new("RGB", (pw, ph), (0, 0, 0))
    g, t, d, b = rect
    box = (int(g * pw), int(t * ph), max(int(d * pw), int(g * pw) + 1), max(int(b * ph), int(t * ph) + 1))
    im.paste((255, 255, 255), box)
    return im


def _surface_blanche(im):
    return float((np.asarray(im.convert("L")) > 127).sum())


def transformer_rectangle(rect, taille_brute, filtres, rotation, orientation):
    """Transforme le rectangle d'un visage (fractions du fichier BRUT) vers la photo exportée,
    en rejouant sur une image-témoin exactement la géométrie de rendre() : recadrage, redressement,
    orientation EXIF, rotation. Retourne ((g, t, d, b) en fractions, cadrage) ou (None, 'hors cadre').
    cadrage : 'entier' / 'partiel' / 'hors cadre'."""
    geo = [f for f in filtres if f[0] in ("crop64", "tilt")]
    temoin = _masque_rect(taille_brute, rect)
    avant = _surface_blanche(temoin)
    recadre = rendre(temoin, [f for f in geo if f[0] == "crop64"], 0, 1)[0] if geo else temoin
    visible = _surface_blanche(recadre) / avant if avant else 0.0
    if visible < 0.05:
        return None, "hors cadre"
    sortie = rendre(temoin, geo, rotation, orientation)[0]
    arr = np.asarray(sortie.convert("L")) > 127
    ys, xs = np.nonzero(arr)
    if not len(xs):
        return None, "hors cadre"
    h, w = arr.shape
    boite = (xs.min() / w, ys.min() / h, (xs.max() + 1) / w, (ys.max() + 1) / h)
    cadrage = "entier" if visible > 0.97 else "partiel"
    return boite, cadrage


# --------------------------------------------------------------------------- rendu des retouches

def vers_rgb(img):
    if img.mode in ("RGB", "L"):
        return img.convert("RGB")
    if img.mode in ("RGBA", "LA", "P"):
        fond = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        fond.paste(rgba, mask=rgba.split()[-1])
        return fond
    return img.convert("RGB")


def f_crop64(img, args):
    h = args[1] if len(args) > 1 else ""
    if len(h) < 16:
        h = h.rjust(16, "0")
    g, t, d, b = (int(h[i:i + 4], 16) / 65535.0 for i in (0, 4, 8, 12))
    w, ht = img.size
    boite = (round(g * w), round(t * ht), round(d * w), round(b * ht))
    if boite[2] - boite[0] < 2 or boite[3] - boite[1] < 2:
        raise ValueError("rectangle de recadrage invalide")
    return img.crop(boite)


# --- images très grandes (scans de 30 millions de pixels et plus) -------------------------------------
# Les calculs en virgule flottante sur toute l'image demandent plusieurs centaines de Mo (erreur « Unable to allocate »).
# On traite alors l'image par bandes horizontales ; les statistiques globales (niveaux de « j'ai de la chance »,
# éclairage/couleur automatiques) sont calculées une fois sur une version réduite de l'image entière.
SEUIL_GRANDE_IMAGE = 12_000_000     # pixels
HAUTEUR_BANDE = 400


def memoire_libre_mo():
    """Mémoire vive disponible en Mo (None si inconnue)."""
    try:
        if os.name == "nt":
            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sull", ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return int(m.ullAvailPhys // 1048576)
        with open("/proc/meminfo") as f:
            for l in f:
                if l.startswith("MemAvailable"):
                    return int(l.split()[1]) // 1024
    except Exception:
        pass
    return None


def regler_memoire():
    """Adapte le traitement par bandes à la mémoire réellement disponible."""
    global SEUIL_GRANDE_IMAGE, HAUTEUR_BANDE
    libre = memoire_libre_mo()
    if libre is None:
        return
    if libre < 1500:
        SEUIL_GRANDE_IMAGE, HAUTEUR_BANDE = 2_000_000, 200
        print(f"Mémoire libre faible ({libre} Mo) : traitement par petites bandes dès 2 Mpx (plus sûr, un peu plus lent). "
              f"Fermer d'autres programmes aide.")
    elif libre < 3000:
        SEUIL_GRANDE_IMAGE, HAUTEUR_BANDE = 6_000_000, 300
        print(f"Mémoire libre : {libre} Mo : bandes dès 6 Mpx.")
_REF = {"img": None}                # image réduite servant aux statistiques globales, pendant un traitement par bandes


def _image_stats(img):
    return _REF["img"] if _REF["img"] is not None else img


def reduire_pour_stats(img, cible=3_000_000):
    k = 1
    while (img.size[0] // k) * (img.size[1] // k) > cible:
        k += 1
    return img.reduce(k) if k > 1 else img


def en_bandes(img, fonction, args, marge=0):
    """Applique fonction(bande, args) bande par bande (avec une marge pour les filtres de voisinage)."""
    w, h = img.size
    sortie = Image.new("RGB", (w, h))
    for y in range(0, h, HAUTEUR_BANDE):
        y0, y1 = max(0, y - marge), min(h, y + HAUTEUR_BANDE + marge)
        res = fonction(img.crop((0, y0, w, y1)), args)
        haut = min(HAUTEUR_BANDE, h - y)
        sortie.paste(res.crop((0, y - y0, w, y - y0 + haut)), (0, y))
        del res
    return sortie


def _bornes_autocontrast(hist, cutoff):
    """Même règle que PIL.ImageOps.autocontrast : on écarte cutoff % aux deux extrémités de l'histogramme."""
    h = list(hist)
    total = sum(h)
    if total <= 0:
        return 0, 255
    cut = total * cutoff / 100.0
    lo = 0
    while lo < 256 and cut > 0:
        if cut > h[lo]:
            cut -= h[lo]; h[lo] = 0
        else:
            h[lo] -= cut; cut = 0
        if cut > 0:
            lo += 1
    cut = total * cutoff / 100.0
    hi = 255
    while hi >= 0 and cut > 0:
        if cut > h[hi]:
            cut -= h[hi]; h[hi] = 0
        else:
            h[hi] -= cut; cut = 0
        if cut > 0:
            hi -= 1
    nz = [i for i, v in enumerate(h) if v > 0]
    if not nz:
        return 0, 255
    return nz[0], nz[-1]


def _lut_etirement(lo, hi):
    if hi <= lo:
        return list(range(256))
    echelle = 255.0 / (hi - lo)
    return [max(0, min(255, int(i * echelle - lo * echelle))) for i in range(256)]


def _autocontrast(img, cutoff, preserve_tone):
    """autocontrast dont les bornes viennent de l'image entière (réduite) même si on ne traite qu'une bande."""
    ref = _image_stats(img).convert("RGB")
    if preserve_tone:
        lo, hi = _bornes_autocontrast(ref.convert("L").histogram(), cutoff)
        lut = _lut_etirement(lo, hi) * 3
    else:
        hist = ref.histogram()
        lut = []
        for c in range(3):
            lo, hi = _bornes_autocontrast(hist[256 * c:256 * (c + 1)], cutoff)
            lut += _lut_etirement(lo, hi)
    return img.convert("RGB").point(lut)


def f_fill(img, args):
    a = float(args[1]) if len(args) > 1 else 0.0
    arr = np.asarray(img, dtype=np.float32) / 255.0
    arr = arr + 0.5 * a * (1.0 - arr) ** 2
    return Image.fromarray((np.clip(arr, 0, 1) * 255 + 0.5).astype(np.uint8))


def f_bw(img, args):
    return img.convert("L").convert("RGB")


_SEPIA_X = np.array([0, 16, 32, 48, 64, 80, 96, 112, 128, 144, 160, 176, 192, 208, 224, 240, 255])
_SEPIA = [np.array([46, 62, 79, 95, 112, 128, 144, 160, 171, 182, 192, 203, 214, 224, 235, 246, 254]),
          np.array([36, 50, 63, 77, 90, 103, 117, 133, 146, 159, 173, 187, 202, 215, 229, 243, 254]),
          np.array([29, 39, 50, 61, 71, 81, 93, 108, 124, 140, 158, 173, 190, 206, 223, 240, 254])]


def f_sepia(img, args):
    """Sépia : couleur fonction de la luminance, relevée sur le panneau de tests (écart ~1,5 niveau)."""
    lum = _pond(np.asarray(img.convert("RGB"), dtype=np.float32), LUMA)
    sortie = np.stack([np.interp(lum, _SEPIA_X, _SEPIA[c]) for c in range(3)], -1)
    return Image.fromarray((sortie + 0.5).astype(np.uint8))


def f_autolight(img, args):
    return _autocontrast(img, 0.5, True)


def f_autocolor(img, args):
    return _autocontrast(img, 0.5, False)


def f_tilt(img, args):
    """Redressement. Calibré sur une photo Picasa (tilt=1,1.0 -> rotation de 0,2 rad = 11,46°,
    sens antihoraire pour une valeur positive, puis zoom minimal pour supprimer les coins vides).
    Hypothèse : l'angle est proportionnel à la valeur (0,2 rad par unité)."""
    valeur = float(args[1]) if len(args) > 1 else 0.0
    if abs(valeur) < 1e-9:
        return img
    rad = 0.2 * valeur
    w, h = img.size
    th = abs(rad)
    zoom = max(math.cos(th) + (h / w) * math.sin(th), math.cos(th) + (w / h) * math.sin(th))
    pivote = img.rotate(math.degrees(rad), resample=Image.BICUBIC, expand=False)
    cw, ch = w / zoom, h / zoom
    boite = ((w - cw) / 2.0, (h - ch) / 2.0, (w + cw) / 2.0, (h + ch) / 2.0)
    return pivote.resize((w, h), Image.BICUBIC, box=boite)


def f_enhance(img, args):
    """« J'ai de la chance » : étirement linéaire des niveaux canal par canal (contraste + couleur automatiques).
    Picasa ne publie pas sa règle ; elle a été retrouvée sur 40 photos avant/après (chaque photo est bien un
    étirement linéaire par canal, au bruit JPEG près). Les bornes mêlent 70 % de statistique propre au canal
    (correction de couleur) et 30 % de statistique commune aux trois canaux (contraste global) :
      point noir  = 0,7 x P0,75 %(canal) + 0,3 x P0,75 %(canal le plus sombre de chaque pixel)
      point blanc = 0,7 x P99,5 %(canal) + 0,3 x P99 %(canal le plus clair de chaque pixel)
    Écart médian ~2 niveaux sur 255 (moyen ~3). Statistiques calculées sur l'image déjà recadrée/redressée."""
    ref = np.asarray(_image_stats(img).convert("RGB"))
    pas = max(1, (ref.shape[0] * ref.shape[1]) // 2_000_000)      # 2 Mpx d'échantillon suffisent
    ech = ref.reshape(-1, 3)[::pas]
    del ref
    centiles = np.percentile(ech, [0.75, 99.5], axis=0)
    noir_commun = float(np.percentile(ech.min(axis=1), 0.75))
    blanc_commun = float(np.percentile(ech.max(axis=1), 99))
    del ech
    luts = []
    x = np.arange(256, dtype=np.float32)
    for c in range(3):
        noir = 0.7 * float(centiles[0, c]) + 0.3 * noir_commun
        blanc = 0.7 * float(centiles[1, c]) + 0.3 * blanc_commun
        if blanc - noir < 2:          # Picasa n'impose pas d'autre limite (une photo noire est très amplifiée)
            blanc = noir + 2
        luts += [int(v) for v in np.clip((x - noir) / (blanc - noir) * 255.0 + 0.5, 0, 255)]
    return img.convert("RGB").point(luts)


LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


def _pond(a, w):
    """Combinaison pondérée des 3 canaux (équivaut à a @ w), sans passer par la bibliothèque BLAS."""
    return a[..., 0] * np.float32(w[0]) + a[..., 1] * np.float32(w[1]) + a[..., 2] * np.float32(w[2])


def _val(args, i):
    try:
        return float(args[i])
    except (IndexError, ValueError):
        return 0.0


def _couleur_argb(texte):
    """'ffaabbcc' (AARRGGBB) -> (r, g, b)"""
    t = (texte or "").rjust(8, "0")[-8:]
    return tuple(int(t[i:i + 2], 16) for i in (2, 4, 6))


def f_finetune2(img, args):
    """Réglage précis : 5 curseurs, calés sur le panneau de tests Picasa (une photo par réglage).
      1 éclairage d'appoint : relève les ombres (courbe calée sur 2 points, approché) ;
      2 hautes lumières     : gain 1/(1-v), écrêtage doux (écart ~1 niveau) ;
      3 ombres              : point noir à 255*v, y = (x-255v)/(1-v) (écart ~1 niveau) ;
      4 retouche rapide des couleurs : pipette « neutre », gains (G/R, 1, G/B) à luminance constante ;
      5 température des couleurs : gains multiplicatifs R/G/B (calé sur 2 valeurs, approché)."""
    p1, p2, p3, p5 = _val(args, 1), _val(args, 2), _val(args, 3), _val(args, 5)
    teinte = args[4] if len(args) > 4 else "00000000"
    y = np.asarray(img, dtype=np.float32)
    if abs(p1) > 1e-9:
        lum = _pond(y, LUMA)
        u = np.clip(lum / 255.0, 0.0, 1.0)
        pp = abs(p1)
        u2 = np.clip(u + 1.107 * (pp ** 1.4146) * (u ** 0.3042) * ((1.0 - u) ** 1.2632), 0.0, 1.0)
        y = np.clip(y + ((u2 - u) * 255.0)[..., None], 0, 255)
    if 1e-9 < p2 < 0.95:
        y = y / (1.0 - p2) - 2.2 * p2 / 0.407
        genou = 225.0
        haut = y > genou
        y[haut] = genou + 30.0 * np.tanh((y[haut] - genou) / 30.0)
        y = np.clip(y, 0, 255)
    if 1e-9 < p3 < 0.95:
        y = np.clip((y - 255.0 * p3) / (1.0 - p3), 0, 255)
    r, g, b = _couleur_argb(teinte)
    if teinte.strip("0") and min(r, g, b) > 0:
        gain = np.array([g / r, 1.0, g / b], dtype=np.float32)
        gain /= float((gain * LUMA).sum())
        y = np.clip(y * gain, 0, 255)
    if abs(p5) > 1e-9:
        if p5 > 0:
            ln = np.array([0.049, -0.005, -0.174]) * p5
        else:
            ln = np.array([-0.235, 0.038, 0.335]) * (-p5)
        y = np.clip(y * np.exp(ln).astype(np.float32), 0, 255)
    return Image.fromarray((y + 0.5).astype(np.uint8))


def reserves_finetune2(args):
    return None


def f_sat(img, args):
    """Saturation : s<0 -> facteur 1+s (écart ~1 niveau) ; s>0 -> ~1+1,5 s (approché)."""
    s = _val(args, 1)
    k = 1.0 + s if s < 0 else 1.0 + 1.49 * s
    a = np.asarray(img, dtype=np.float32)
    g = _pond(a, LUMA)[..., None]
    return Image.fromarray((np.clip(g + k * (a - g), 0, 255) + 0.5).astype(np.uint8))


def f_ansel(img, args):
    """N&B filtré : gris = somme des canaux pondérée par la couleur du filtre (R,G,B / somme)."""
    c = np.array(_couleur_argb(args[1] if len(args) > 1 else "ffffffff"), dtype=np.float32)
    if c.sum() < 1:
        c = np.array([1, 1, 1], dtype=np.float32)
    g = _pond(np.asarray(img, dtype=np.float32), c / c.sum())
    return Image.fromarray((np.repeat(g[..., None], 3, 2) + 0.5).astype(np.uint8))


_NEON_M = np.array([0.93, 1.32, 1.49, 1.62, 1.74, 1.85, 1.96, 2.08, 2.21, 2.36, 2.53, 2.73, 2.98, 3.31, 3.74, 4.32, 5.13, 6.34, 8.38, 13.32, 30.26])
_NEON_R = np.array([0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 4, 7, 13, 21, 35, 55, 90, 156, 245, 254])
_NEON_G = np.array([0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 51, 237])


def f_neon(img, args):
    """Néon : contours colorés sur fond noir. Force des contours = gradient RGB (flou gaussien 1 px),
    convertie en intensité par une courbe mesurée sur un export Picasa ; les contours les plus forts
    débordent vers le blanc. args = (1, force?, couleur AARRGGBB) ; la couleur est prise en compte,
    le 2e paramètre (0.0 dans l'exemple) est ignoré."""
    rgb = img.convert("RGB")
    carres = 0
    for canal in rgb.split():
        b = np.asarray(canal.filter(ImageFilter.GaussianBlur(1.0))).astype(np.float32)
        gx = np.zeros_like(b)
        gy = np.zeros_like(b)
        gx[:, 1:-1] = (b[:, 2:] - b[:, :-2]) / 2
        gy[1:-1] = (b[2:] - b[:-2]) / 2
        carres = carres + gx * gx + gy * gy
    m = np.sqrt(carres)
    inten = np.interp(m, _NEON_M, _NEON_R) / 255.0     # 0..1 : couleur pleine à 1
    blanc = np.interp(m, _NEON_M, _NEON_G) / 255.0     # 0..1 : débordement vers le blanc
    coul = np.array(_couleur_argb(args[2] if len(args) > 2 else "00ff0000"), dtype=np.float32)
    base = coul[None, None, :] * inten[..., None]
    sortie = base + (255.0 - base) * blanc[..., None]
    return Image.fromarray(np.clip(sortie + 0.5, 0, 255).astype(np.uint8), "RGB")


def f_invert(img, args):
    return ImageOps.invert(img.convert("RGB"))


_WARM = (np.array([0, 8, 16, 32, 64, 96, 128, 160, 192, 224, 248, 255]),
         [np.array([1, 10, 20, 40, 79, 112, 139, 163, 187, 214, 236, 243]),
          np.array([0, 7, 14, 29, 60, 88, 113, 138, 168, 200, 225, 232]),
          np.array([0, 5, 11, 21, 44, 69, 97, 128, 162, 198, 226, 233])])


def f_warm(img, args):
    """Réchauffer : courbes par canal relevées sur le panneau de tests (écart ~1 niveau)."""
    a = np.asarray(img, dtype=np.float32)
    sortie = np.empty_like(a)
    for c in range(3):
        sortie[..., c] = np.interp(a[..., c], _WARM[0], _WARM[1][c])
    return Image.fromarray((sortie + 0.5).astype(np.uint8))


def f_unsharp2(img, args):
    """Netteté : masque flou (sigma 1,5) ; réglage approché (calé sur 1 valeur extrême)."""
    v = max(0.0, _val(args, 1))
    a = np.asarray(img, dtype=np.float32)
    flou = np.asarray(img.filter(ImageFilter.GaussianBlur(1.5)), dtype=np.float32)
    return Image.fromarray((np.clip(a + 0.16 * v * (a - flou) + 1.0 * v, 0, 255) + 0.5).astype(np.uint8))


def corriger_yeux_rouges(img, zones=None):
    """Yeux rouges. Picasa ne stocke aucune zone (redeye=1) : détection des pupilles rouges.
    zones : rectangles de visages (g, t, d, b) en fractions ; sinon on exige une paire d'yeux."""
    a = np.asarray(img.convert("RGB"), dtype=np.float32)
    h, w, _ = a.shape
    R, G, B = a[..., 0], a[..., 1], a[..., 2]
    cand = (R > 100) & (G < 0.40 * R) & (B < 0.95 * R)
    if zones:
        zm = np.zeros((h, w), bool)
        for g, t, d, b in zones:
            fw, fh = d - g, b - t
            x0, x1 = int((g + 0.08 * fw) * w), int((d - 0.08 * fw) * w)
            y0, y1 = int((t + 0.15 * fh) * h), int((t + 0.60 * fh) * h)
            zm[max(y0, 0):y1, max(x0, 0):x1] = True
        cand &= zm
    ys, xs = np.nonzero(cand)
    vu = np.zeros((h, w), bool)
    amax = (0.04 * w) ** 2
    taches = []
    for y0, x0 in zip(ys, xs):
        if vu[y0, x0]:
            continue
        file = deque([(y0, x0)]); vu[y0, x0] = True; pts = []
        while file and len(pts) <= 4 * amax:
            y, x = file.popleft(); pts.append((y, x))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    yy, xx = y + dy, x + dx
                    if 0 <= yy < h and 0 <= xx < w and cand[yy, xx] and not vu[yy, xx]:
                        vu[yy, xx] = True; file.append((yy, xx))
        n = len(pts)
        if n < 20 or n > amax:
            continue
        py = [p[0] for p in pts]; px = [p[1] for p in pts]
        bh, bw = max(py) - min(py) + 1, max(px) - min(px) + 1
        if not (0.55 < bw / bh < 1.8) or n / (bh * bw) < 0.55:
            continue
        taches.append((float(np.mean(py)), float(np.mean(px)), n, pts))
    if not zones:   # sans visage connu : il faut une paire de pupilles comparables
        gardees = []
        for i, (cy, cx, n, pts) in enumerate(taches):
            diam = 2 * np.sqrt(n / np.pi)
            for j, (cy2, cx2, n2, _) in enumerate(taches):
                if i == j:
                    continue
                dist = np.hypot(cy - cy2, cx - cx2)
                if 0.4 < n2 / n < 2.5 and 2.5 * diam < dist < 14 * diam and abs(cy - cy2) < 0.5 * dist:
                    gardees.append(taches[i]); break
        taches = gardees
    if not taches:
        return img
    masque = np.zeros((h, w), bool)
    for cy, cx, n, pts in taches:
        rr = 1.35 * np.sqrt(n / np.pi) + 2.0
        y0_, y1_ = int(max(cy - rr, 0)), int(min(cy + rr + 1, h))
        x0_, x1_ = int(max(cx - rr, 0)), int(min(cx + rr + 1, w))
        yy, xx = np.mgrid[y0_:y1_, x0_:x1_]
        disque = (yy - cy) ** 2 + (xx - cx) ** 2 <= rr * rr
        souple = (R[y0_:y1_, x0_:x1_] > 1.15 * np.maximum(G, B)[y0_:y1_, x0_:x1_]) & (R[y0_:y1_, x0_:x1_] > 70)
        masque[y0_:y1_, x0_:x1_] |= disque & souple
        for y, x in pts:
            masque[y, x] = True
    sortie = a.copy()
    nr = 0.009 * R + 0.751 * G + 0.29 * B - 1.7
    ng = 0.053 * R + 0.737 * G + 0.271 * B - 7.7
    nb = 0.031 * R + 0.665 * G + 0.375 * B - 7.0
    sortie[masque] = np.clip(np.stack([nr, ng, nb], -1)[masque], 0, 255)
    return Image.fromarray((sortie + 0.5).astype(np.uint8))


def f_redeye(img, args, zones=None):
    return corriger_yeux_rouges(img, zones)


# nom -> (fonction, exact ?[, réserves])  ; exact=False : rendu approché (algorithme Picasa non public)
# réserves(args) renvoie un texte si une partie du réglage n'est pas reproduite.
FILTRES = {
    "finetune2": (f_finetune2, False, reserves_finetune2),
    "tilt": (f_tilt, True),
    "enhance": (f_enhance, False),
    "crop64": (f_crop64, True),
    "bw": (f_bw, False),
    "sepia": (f_sepia, False),
    "fill": (f_fill, False),
    "autolight": (f_autolight, False),
    "autocolor": (f_autocolor, False),
    "sat": (f_sat, False),
    "ansel": (f_ansel, False),
    "invert": (f_invert, True),
    "warm": (f_warm, False),
    "unsharp2": (f_unsharp2, False),
    "redeye": (f_redeye, False),
    "neon": (f_neon, False),
}


ORIENTATIONS_EXIF = {
    2: [Image.FLIP_LEFT_RIGHT], 3: [Image.ROTATE_180], 4: [Image.FLIP_TOP_BOTTOM],
    5: [Image.TRANSPOSE], 6: [Image.ROTATE_270], 7: [Image.TRANSVERSE], 8: [Image.ROTATE_90],
}


def appliquer_orientation(img, orientation, rotation):
    for op in ORIENTATIONS_EXIF.get(orientation, []):
        img = img.transpose(op)
    if rotation:
        img = img.rotate(-90 * rotation, expand=True)  # rotate(1) = 90° horaire
    return img


# filtres exécutables bande par bande -> marge (lignes) nécessaire pour les filtres de voisinage
FILTRES_PAR_BANDES = {"finetune2": 0, "enhance": 0, "bw": 0, "sepia": 0, "fill": 0, "autolight": 0,
                      "autocolor": 0, "sat": 0, "ansel": 0, "invert": 0, "warm": 0, "unsharp2": 8, "neon": 8}


def rendre(img, filtres, rotation, orientation=1, zones=None):
    """Applique les filtres dans l'ordre, dans le repère BRUT du fichier (les recadrages de Picasa
    y sont exprimés, avant l'orientation EXIF), puis l'orientation d'affichage (EXIF, puis rotate()).
    Retourne (img, appliqués, approchés, ignorés)."""
    appliques, approches, ignores = [], [], []
    img = vers_rgb(img)
    # Calibré sur une photo Picasa : le redressement (tilt) s'applique à la zone recadrée, autour de
    # son centre, avec un zoom calculé sur ses proportions. On exécute donc crop64 AVANT tilt,
    # quel que soit l'ordre d'écriture dans le .picasa.ini.
    if any(n == "tilt" for n, _ in filtres) and any(n == "crop64" for n, _ in filtres):
        sans_tilt = [f for f in filtres if f[0] != "tilt"]
        dernier_crop = max(i for i, f in enumerate(sans_tilt) if f[0] == "crop64")
        filtres = (sans_tilt[:dernier_crop + 1] + [f for f in filtres if f[0] == "tilt"]
                   + sans_tilt[dernier_crop + 1:])
    # yeux rouges : détecté dans le repère brut, donc AVANT tout recadrage
    filtres = [f for f in filtres if f[0] == "redeye"] + [f for f in filtres if f[0] != "redeye"]
    for nom, args in filtres:
        if args and args[0] == "0":
            continue  # filtre désactivé
        if nom not in FILTRES:
            ignores.append(nom)
            continue
        fonction, exact = FILTRES[nom][0], FILTRES[nom][1]
        try:
            if nom == "redeye":
                img = fonction(img, args, zones)
            elif nom in FILTRES_PAR_BANDES:
                grande = img.size[0] * img.size[1] > SEUIL_GRANDE_IMAGE
                try:
                    if grande:
                        raise MemoryError("image très grande : traitement par bandes")
                    img = fonction(img, args)
                except MemoryError:
                    _REF["img"] = reduire_pour_stats(img)
                    try:
                        img = en_bandes(img, fonction, args, FILTRES_PAR_BANDES[nom])
                    finally:
                        _REF["img"] = None
            else:
                img = fonction(img, args)
            appliques.append(nom)
            if not exact:
                approches.append(nom)
            if len(FILTRES[nom]) > 2:
                note = FILTRES[nom][2](args)
                if note:
                    ignores.append(note)
        except Exception as e:  # noqa: BLE001
            ignores.append(f"{nom}(erreur: {e})")
    if orientation != 1 or rotation:
        img = appliquer_orientation(img, orientation, rotation)
        if rotation:
            appliques.append(f"rotate({rotation})")
    return img, appliques, approches, ignores


# --------------------------------------------------------------------------- fichiers & dates

def copier_fichier(src, dst):
    """Copie avec dates ; si une date est invalide (fréquent sur de vieux disques), copie sans les dates."""
    try:
        shutil.copy2(src, dst)
    except (OSError, ValueError, OverflowError):
        if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
            shutil.copyfile(src, dst)
        copier_dates(src, dst)


def copier_dates(src, dst):
    """Recopie dates d'accès, de modification et (Windows) de création, y compris avant 1970."""
    st = os.stat(src)
    ok = True
    try:
        os.utime(dst, (st.st_atime, st.st_mtime))
    except (OSError, ValueError, OverflowError):
        ok = False
    if os.name == "nt":
        try:
            _fixer_dates_windows(dst, st.st_ctime, None if ok else st.st_atime, None if ok else st.st_mtime)
        except Exception:  # noqa: BLE001
            pass


def _filetime(ts):
    from ctypes import wintypes
    ft = int((ts + 11644473600) * 10000000)   # intervalles de 100 ns depuis 1601 : accepte les dates avant 1970
    return wintypes.FILETIME(ft & 0xFFFFFFFF, ft >> 32)


def _fixer_dates_windows(chemin, creation, acces=None, modif=None):
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = wintypes.HANDLE
    h = k32.CreateFileW(chemin, 0x100, 0, None, 3, 0x80, None)  # FILE_WRITE_ATTRIBUTES
    if h in (-1, 0, None):
        return
    c = _filetime(creation)
    a = _filetime(acces) if acces is not None else None
    m = _filetime(modif) if modif is not None else None
    k32.SetFileTime(h, ctypes.byref(c), ctypes.byref(a) if a else None, ctypes.byref(m) if m else None)
    k32.CloseHandle(h)


def date_fichier(chemin):
    """Date de modification du fichier (heure locale). Les dates d'avant 1970 (photos anciennes) font échouer
    datetime.fromtimestamp sous Windows : on calcule alors à partir de l'époque 1970."""
    try:
        t = os.path.getmtime(chemin)
    except OSError:
        return ""
    try:
        return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        try:
            return (datetime(1970, 1, 1) + timedelta(seconds=t)).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, ValueError):
            return ""


def date_prise_vue(chemin):
    try:
        with Image.open(chemin) as im:
            ex = im.getexif()
            v = ex.get_ifd(0x8769).get(0x9003) or ex.get(0x0132)
            if v:
                return datetime.strptime(str(v)[:19], "%Y:%m:%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        pass
    return date_fichier(chemin)


# Modifications que Picasa enregistre dans la photo du dossier quand il crée un original dans .picasaoriginals
FILTRES_CUITS = {"redeye", "retouch"}


VERSION_ETAT = 2
# réglages Picasa qui ne changent pas l'image : s'ils sont seuls à changer, seul le catalogue est mis à jour
CLES_CATALOGUE = {"faces", "star", "caption", "keywords", "albums"}


def liste_dossier(dossier):
    """{nom: (taille, date)} des fichiers d'un dossier, en une seule lecture (sous Windows, la taille et la date
    sont fournies avec la liste : pas d'accès disque par fichier, précieux sur un disque externe)."""
    res = {}
    try:
        with os.scandir(dossier) as it:
            for e in it:
                try:
                    if e.is_file():
                        stt = e.stat()
                        res[e.name] = (stt.st_size, int(stt.st_mtime))
                except OSError:
                    res[e.name] = None
    except OSError:
        pass
    return res


def signature_rendu(st, sec, sauvegarde):
    """Empreinte de ce qui détermine la photo exportée : taille + date du fichier, réglages Picasa de CETTE photo
    (sa section [nom] du .picasa.ini, hors personnes/étoile/légende) et original sauvegardé. Picasa n'écrit
    pour chaque photo que sa propre section : on sait donc exactement quelle photo a été modifiée."""
    morceaux = [f"{st[0]}:{st[1]}"]
    morceaux.append(repr(sorted((k, v) for k, v in sec.items() if k not in CLES_CATALOGUE)))
    if sauvegarde:
        try:
            sb = os.stat(sauvegarde)
            morceaux.append(f"{sb.st_size}:{int(sb.st_mtime)}")
        except OSError:
            morceaux.append("?")
    return hashlib.sha1("|".join(morceaux).encode("utf-8", "replace")).hexdigest()


_CACHE_ORIGINAUX = {}


def trouver_sauvegarde(dossier, nom):
    """Cherche l'original sauvegardé par Picasa ; le contenu de .picasaoriginals n'est lu qu'une fois par dossier
    (important sur un disque dur externe lent)."""
    rep = os.path.join(dossier, ".picasaoriginals")
    if rep not in _CACHE_ORIGINAUX:
        try:
            _CACHE_ORIGINAUX[rep] = os.listdir(rep)
        except OSError:
            _CACHE_ORIGINAUX[rep] = None
    contenu = _CACHE_ORIGINAUX[rep]
    if contenu is None:
        return None
    if nom in contenu:
        return os.path.join(rep, nom)
    base, ext = os.path.splitext(nom)
    candidats = sorted(f for f in contenu
                       if f.lower().startswith(base.lower()) and f.lower().endswith(ext.lower()))
    return os.path.join(rep, candidats[0]) if candidats else None


# --- photos abîmées (fichier tronqué ou corrompu) ------------------------------------------------
def analyser_jpeg(chemin):
    """Contrôle de structure sans décodage : cherche, dans les données compressées de l'image principale,
    des marqueurs JPEG impossibles (octet FF suivi d'autre chose que 00/RSTn/FF). Retourne
    (ok, fraction_saine, detail) ; fraction_saine = part des données compressées située avant la première anomalie."""
    with open(chemin, "rb") as f:
        d = f.read()
    if d[:2] != b"\xff\xd8":
        return False, 0.0, "pas un JPEG valide (en-tête absent)"
    # parcours des segments d'en-tête jusqu'au premier SOS
    i, prog = 2, False
    while i + 4 <= len(d) and d[i] == 0xFF:
        m = d[i + 1]
        if m == 0xDA:
            break
        if m in (0xC2, 0xC6, 0xCA, 0xCE):
            prog = True
        i += 2 + struct.unpack(">H", d[i + 2:i + 4])[0]
    else:
        return False, 0.0, "en-tête JPEG illisible"
    debut = i + 2 + struct.unpack(">H", d[i + 2:i + 4])[0]
    autorises = b"\x00\xd0\xd1\xd2\xd3\xd4\xd5\xd6\xd7\xff" + (b"\xc4\xda\xdd" if prog else b"")
    fin = d.rfind(b"\xff\xd9")
    if fin < debut:
        return False, 0.0, "fin de fichier absente (EOI manquant)"
    # un marqueur FF D9 en plein milieu des données = fin prématurée ; on le cherche d'abord
    zone = d[debut:fin]
    bad = [m.start() for m in re.finditer(b"\xff[^" + re.escape(autorises) + b"]", zone)]
    # E1..EF / FE (métadonnées) ne sont jamais légitimes dans les données compressées
    if len(bad) >= 3 or any(zone[b + 1] == 0xD9 for b in bad):
        premier = bad[0]
        return False, premier / max(len(zone), 1), f"données JPEG corrompues vers {100 * premier / max(len(zone), 1):.0f} % du fichier"
    zeros = re.search(b"\x00{4096,}", zone)
    if zeros:
        return False, zeros.start() / max(len(zone), 1), f"zone vide dans les données vers {100 * zeros.start() / max(len(zone), 1):.0f} % du fichier"
    return True, 1.0, ""


def miniature_exif(chemin):
    with open(chemin, "rb") as f:
        d = f.read(300000)
    fin_app1 = 4 + struct.unpack(">H", d[4:6])[0] if d[2:4] == b"\xff\xe1" else 0
    if not fin_app1:
        return None
    seg = d[6:fin_app1]
    j = seg.find(b"\xff\xd8\xff", 12)
    if j < 0:
        return None
    k = seg.rfind(b"\xff\xd9")
    try:
        im = Image.open(io.BytesIO(seg[j:k + 2])); im.load()
        return im.convert("RGB")
    except Exception:
        return None


def reconstruire_depuis_miniature(chemin):
    """Décode ce qui est lisible puis complète le bas de l'image avec la miniature intégrée (agrandie).
    Retourne (image, fraction_lisible) ou None."""
    mini = miniature_exif(chemin)
    if mini is None:
        return None
    with Image.open(chemin) as im:
        im.load()
        img = im.convert("RGB")
    w, h = img.size
    a = np.asarray(img)
    # première ligne « vide » (gris uni 128 laissé par le décodeur) jusqu'en bas
    uni = (np.abs(a.astype(np.int16) - 128).max(axis=(1, 2)) <= 1)
    coupe = h
    for y in range(h - 1, -1, -1):
        if uni[y]:
            coupe = y
        else:
            break
    if coupe >= h - 2:
        return None          # rien ne manque en bas
    # le dernier groupe de lignes décodées est souvent abîmé : on recule tant qu'il s'écarte de la miniature
    tw, th = mini.size
    pet = np.asarray(img.resize((tw, th), Image.BOX)).astype(np.float32)
    mn = np.asarray(mini).astype(np.float32)
    ecart = np.abs(pet - mn).mean(axis=(1, 2))
    seuil = max(8.0, 3 * float(np.median(ecart[: max(2, int(coupe * th / h) - 1)])))
    r = int(coupe * th / h) - 1
    while r > 0 and ecart[r] > seuil:
        r -= 1
    coupe = max(0, min(coupe, int(r * h / th) - 8))
    grande = mini.resize((w, h), Image.LANCZOS)
    sortie = img.copy()
    zone = grande.crop((0, coupe, w, h))
    sortie.paste(zone, (0, coupe))
    # fondu de 24 lignes pour masquer la couture
    f = min(24, coupe)
    if f > 0:
        haut = img.crop((0, coupe - f, w, coupe)); bas = grande.crop((0, coupe - f, w, coupe))
        masque = Image.fromarray((np.linspace(0, 255, f)[:, None] * np.ones((1, w))).astype(np.uint8), "L")
        sortie.paste(Image.composite(bas, haut, masque), (0, coupe - f))
    return sortie, coupe / h


def enregistrer_rendu(img, src, dst, qualite):
    ext = os.path.splitext(dst)[1].lower()
    options = {}
    with Image.open(src) as orig:
        exif = orig.getexif()
        if 0x0112 in exif:
            exif[0x0112] = 1  # l'orientation est déjà appliquée aux pixels
        if exif and ext in (".jpg", ".jpeg", ".png", ".tif", ".tiff"):
            options["exif"] = exif.tobytes()
        icc = orig.info.get("icc_profile")
        if icc:
            options["icc_profile"] = icc
    if ext in (".jpg", ".jpeg"):
        img.save(dst, "JPEG", quality=qualite, subsampling=0, optimize=True, **options)
    elif ext == ".png":
        img.save(dst, "PNG", **options)
    elif ext in (".tif", ".tiff"):
        img.save(dst, "TIFF", **options)
    else:
        img.save(dst)


def rendre_vers(src, dst, filtres, rotation, qualite, zones=None):
    with Image.open(src) as im:
        orientation = im.getexif().get(0x0112, 1)
        im.load()
        img, ap, apx, ig = rendre(im, filtres, rotation, orientation, zones)
    enregistrer_rendu(img, src, dst, qualite)
    copier_dates(src, dst)
    return ap, apx, ig


def marquer_exiftool(exiftool, chemin, personnes, favori):
    if not (personnes or favori):
        return
    cmd = [exiftool, "-q", "-overwrite_original", "-P", "-charset", "utf8",
           "-charset", "filename=utf8"]
    for p in personnes:
        cmd += [f"-XMP-iptcExt:PersonInImage+={p}", f"-XMP-dc:Subject+={p}"]
    if favori:
        cmd += ["-XMP:Rating=5", "-XMP-dc:Subject+=Favori"]
    cmd.append(chemin)
    subprocess.run(cmd, check=False, capture_output=True)


# --------------------------------------------------------------------------- traitement principal

def executer(a):
    src_root = os.path.abspath(a.source)
    dst_root = os.path.abspath(a.sortie)
    if not os.path.isdir(src_root):
        print(f"Dossier source introuvable : {src_root}")
        return 1
    if os.path.normcase(dst_root).startswith(os.path.normcase(src_root) + os.sep) or \
            os.path.normcase(dst_root) == os.path.normcase(src_root):
        print("Le dossier de sortie ne doit pas être dans le dossier source (ni identique).")
        return 1

    regler_memoire()
    exiftool = None if a.sans_exiftool else shutil.which("exiftool")
    glob_id, glob_focus = contacts_globaux(a.contacts)
    print(f"Source : {src_root}\nSortie : {dst_root}")
    print(f"exiftool : {'oui' if exiftool else 'non (personnes/favoris seulement dans le catalogue)'}")
    print(f"Noms de contacts globaux lus (contacts.xml) : {len(glob_id)}")
    if a.simulation:
        print("MODE SIMULATION : rien ne sera écrit.")
    _bits = 64 if sys.maxsize > 2**32 else 32
    if _bits == 32:
        print("ATTENTION : Python 32 bits détecté (mémoire limitée à ~2 Go). "
              "Installez Python 64 bits pour traiter les très grandes images.")

    # --- relance incrémentale : état de la dernière extraction
    cat = os.path.join(dst_root, "_Catalogue_Picasa")
    fichier_etat = os.path.join(cat, "etat_export.json")
    etat_prec = {}
    try:
        with open(fichier_etat, encoding="utf-8") as f:
            contenu = json.load(f)
        if isinstance(contenu, dict) and contenu.get("version") == VERSION_ETAT:
            etat_prec = contenu.get("fichiers", {})
            print(f"Extraction précédente trouvée : {len(etat_prec)} fichiers connus (mode incrémental).")
        else:
            print("État d'une version antérieure du programme : tout sera refait une fois.")
    except (OSError, ValueError):
        pass
    etat = {}
    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_nouveaux = n_maj = n_inchanges = n_reprises = n_catalogue = 0

    lignes = []
    par_personne = defaultdict(list)
    lignes_visages = []
    favoris = []
    n_total = n_modif = n_erreurs = n_edit_traitees = 0
    t0 = time.time()
    derniere_sauvegarde = time.time()
    interrompu = False

    def sauver_etat():
        """Écrit l'état (fichiers traités + fichiers pas encore revus) ; écriture atomique."""
        if a.simulation:
            return
        os.makedirs(cat, exist_ok=True)
        tout = dict(etat_prec)
        tout.update(etat)
        tmp = fichier_etat + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": VERSION_ETAT, "fichiers": tout}, f, ensure_ascii=False)
        os.replace(tmp, fichier_etat)

    try:
        for racine, dossiers, _fichiers in os.walk(src_root):
            dossiers[:] = sorted(d for d in dossiers if d.lower() not in DOSSIERS_IGNORES)
            rel = os.path.relpath(racine, src_root)
            rel = "" if rel == "." else rel
            sections, contacts_loc, liens = lire_ini(racine)
            contacts = noms_du_dossier(contacts_loc, liens, glob_id, glob_focus)
            dest_dir = os.path.join(dst_root, rel)
            # une seule lecture de chaque dossier (source et sortie) : sous Windows, la date et la taille
            # des fichiers sont fournies avec la liste, sans accès supplémentaire au disque
            entrees = liste_dossier(racine)
            deja_en_sortie = set(liste_dossier(dest_dir))

            for nom in sorted(entrees):
                st = entrees[nom]
                if st is None or nom.lower() in FICHIERS_IGNORES:
                    continue
                src = os.path.join(racine, nom)
                ext = os.path.splitext(nom)[1].lower()
                n_total += 1
                sec = sections.get(nom.lower(), {})
                est_image = ext in EXT_IMAGES
                filtres = parser_filtres(sec.get("filters")) if est_image else []
                rotation = parser_rotation(sec.get("rotate")) if est_image else 0
                if est_image:
                    for nom_f, args in filtres:
                        if nom_f == "rotate":
                            rotation = (rotation + parser_rotation(f"rotate({args[0] if args else 0})")) % 4
                    filtres = [f for f in filtres if f[0] != "rotate"]
                sauvegarde = trouver_sauvegarde(racine, nom) if est_image else None
                deja_cuites = []
                if sauvegarde and not a.rejouer:
                    # Original dans .picasaoriginals : la photo du dossier contient DÉJÀ les modifications
                    # « cuites » par Picasa (yeux rouges, retouches). Les autres réglages du .picasa.ini
                    # (recadrage, redressement, effets, rotation…) sont virtuels : on les rejoue sur la photo.
                    deja_cuites = [f[0] for f in filtres if f[0] in FILTRES_CUITS]
                    filtres = [f for f in filtres if f[0] not in FILTRES_CUITS]
                personnes = parser_visages(sec.get("faces"), contacts) if est_image else []
                favori = sec.get("star", "").lower() == "yes"
                legende = sec.get("caption", "")
                visages_bruts = sec.get("faces", "")
                modifiee = bool(filtres or rotation or sauvegarde)
                if modifiee:
                    n_modif += 1

                if a.echantillon and modifiee and n_edit_traitees >= a.echantillon:
                    print(f"Échantillon de {a.echantillon} photos retouchées atteint.")
                    raise StopIteration

                dst = os.path.join(dest_dir, nom)
                chemin_rel = os.path.join(rel, nom) if rel else nom
                cle = chemin_rel.replace(os.sep, "/")
                sig = signature_rendu(st, sec, sauvegarde)
                prec = etat_prec.get(cle)
                r_prec = (prec or {}).get("row") or []

                # ---- que faire de ce fichier ?
                if a.ecraser or not prec or not r_prec:
                    action, motif = "nouveau", ""
                elif nom not in deja_en_sortie:
                    action, motif = "refaire", "fichier absent de la sortie"
                elif prec.get("sig") != sig:
                    action, motif = "maj", ""
                elif str(r_prec[10]).startswith("ERREUR") or "(erreur" in str(r_prec[8]):
                    action, motif = "refaire", "erreur lors de l'extraction précédente"
                else:
                    action, motif = "inchange", ""

                idx_visages = len(lignes_visages)
                statut, appliques, approches, ignores = "copiée", [], [], []
                integrite = r_prec[14] if len(r_prec) > 14 else ""
                dommage = rec = None
                try:
                    if action == "inchange":
                        n_inchanges += 1
                        if exiftool and est_image and not a.simulation and \
                                (prec.get("personnes") != personnes or r_prec[2] != ("oui" if favori else "non")):
                            marquer_exiftool(exiftool, dst, personnes, favori)
                    else:
                        a_rendre = bool(filtres or rotation)
                        if est_image and ext in (".jpg", ".jpeg") and a.verifier:
                            ok_j, frac_j, detail_j = analyser_jpeg(src)
                            if ok_j:
                                integrite = "saine"
                            else:
                                dommage = (frac_j, detail_j)
                                integrite = "ENDOMMAGÉE : " + detail_j
                        if not a.simulation:
                            os.makedirs(dest_dir, exist_ok=True)
                            if dommage and a.reconstruire:
                                try:
                                    rec = reconstruire_depuis_miniature(src)
                                except Exception:  # noqa: BLE001
                                    rec = None
                            if rec is not None:
                                img0, lisible = rec
                                with Image.open(src) as im_o:
                                    orient0 = im_o.getexif().get(0x0112, 1)
                                img_r, appliques, approches, ignores = rendre(img0, filtres, rotation, orient0,
                                                                              rectangles_visages(visages_bruts))
                                enregistrer_rendu(img_r, src, dst, a.qualite)
                                copier_dates(src, dst)
                                statut = f"reconstruite à partir de la miniature (partie lisible : {lisible * 100:.0f} %)"
                                integrite += f" ; reconstruite avec la miniature intégrée au-delà de {lisible * 100:.0f} % de la hauteur"
                                modifiee = True
                            elif a_rendre:
                                appliques, approches, ignores = rendre_vers(src, dst, filtres, rotation, a.qualite,
                                                                            rectangles_visages(visages_bruts))
                                if appliques:
                                    statut = "retouches rendues"
                                else:
                                    copier_fichier(src, dst)
                                    statut = "copiée (aucune retouche applicable)"
                            else:
                                copier_fichier(src, dst)
                                statut = "copiée (retouche déjà enregistrée)" if sauvegarde else "copiée"
                                if dommage:
                                    statut += " — fichier source abîmé, non réparable"
                            if a.originaux and modifiee:
                                orig_dir = os.path.join(dest_dir, a.sous_dossier_originaux)
                                os.makedirs(orig_dir, exist_ok=True)
                                orig_dst = os.path.join(orig_dir, nom)
                                if action != "inchange" or not os.path.exists(orig_dst):
                                    copier_fichier(sauvegarde or src, orig_dst)
                            if exiftool and est_image:
                                marquer_exiftool(exiftool, dst, personnes, favori)
                        else:
                            if a_rendre:
                                appliques = [f[0] for f in filtres if f[0] in FILTRES]
                                approches = [f[0] for f in filtres if f[0] in FILTRES and not FILTRES[f[0]][1]]
                                ignores = [f[0] for f in filtres if f[0] not in FILTRES]
                            statut = "(simulation)"
                        if modifiee:
                            n_edit_traitees += 1
                except Exception as e:  # noqa: BLE001
                    n_erreurs += 1
                    statut = f"ERREUR : {e}"
                    if not a.simulation and not os.path.exists(dst):
                        try:     # copie de secours : la photo ne doit pas manquer dans la sortie
                            os.makedirs(dest_dir, exist_ok=True)
                            copier_fichier(src, dst)
                            statut += " (fichier copié tel quel)"
                        except Exception:  # noqa: BLE001
                            pass

                # ---- ligne du catalogue
                if action == "inchange":
                    # photo inchangée : on garde la ligne précédente (date, retouches, statut) ;
                    # personnes / favori / légende sont relus dans le .picasa.ini
                    ligne_cat = list(r_prec)
                    if (ligne_cat[2], ligne_cat[3], ligne_cat[9]) != ("oui" if favori else "non", "; ".join(personnes), legende):
                        n_catalogue += 1
                    ligne_cat[2], ligne_cat[3], ligne_cat[9] = "oui" if favori else "non", "; ".join(personnes), legende
                    date = ligne_cat[1]
                else:
                    date = date_prise_vue(src) if est_image else date_fichier(src)
                    ignores_txt = ignores + (["déjà enregistrés dans la photo, non rejoués : " + ", ".join(deja_cuites)]
                                             if deja_cuites else [])
                    ligne_cat = [chemin_rel, date, "oui" if favori else "non", "; ".join(personnes),
                                 "oui" if modifiee else "non", "oui" if sauvegarde else "non",
                                 ", ".join(appliques), ", ".join(approches), ", ".join(ignores_txt), legende, statut]
                    n_maj_prec = int(r_prec[12] or 0) if len(r_prec) > 12 else 0
                    der_maj_prec = r_prec[13] if len(r_prec) > 13 else ""
                    if action == "maj":
                        n_maj += 1
                        ligne_cat[10] += f" ; mise à jour le {maintenant[:10]} (modifiée dans Picasa depuis la dernière extraction)"
                        ligne_cat += [maintenant, n_maj_prec + 1, maintenant]
                    elif action == "refaire":
                        n_reprises += 1
                        ligne_cat[10] += f" (refaite : {motif})"
                        ligne_cat += [maintenant, n_maj_prec, der_maj_prec]
                    else:
                        n_nouveaux += 1
                        ligne_cat += [maintenant, 0, ""]
                    ligne_cat.append(integrite)
                lignes.append(ligne_cat)
                for p in personnes:
                    par_personne[p].append(chemin_rel)

                # ---- visages : recalculés seulement si la photo ou ses visages ont changé
                if action == "inchange" and prec.get("faces", "") == visages_bruts:
                    for v in prec.get("visages", []):
                        v = list(v)
                        v[3] = "oui" if favori else "non"
                        lignes_visages.append(v)
                elif est_image and visages_bruts:
                    try:
                        with Image.open(src) as im0:
                            brute = im0.size
                            orient = im0.getexif().get(0x0112, 1)
                        taille_sortie = None
                        if not a.simulation and os.path.exists(dst):
                            with Image.open(dst) as imd:
                                taille_sortie = imd.size
                        for nom_p, rect in visages_detail(visages_bruts, contacts):
                            boite, cadrage = transformer_rectangle(rect, brute, filtres, rotation, orient)
                            ligne = [nom_p or "(non identifié)", chemin_rel, date, "oui" if favori else "non"]
                            if boite:
                                gg, tt, dd, bb = boite
                                ligne += [f"{gg:.4f}", f"{tt:.4f}", f"{dd - gg:.4f}", f"{bb - tt:.4f}"]
                                if taille_sortie:
                                    tw, th = taille_sortie
                                    ligne += [tw, th, round(gg * tw), round(tt * th), round((dd - gg) * tw), round((bb - tt) * th)]
                                else:
                                    ligne += ["", "", "", "", "", ""]
                            else:
                                ligne += [""] * 10
                            ligne += [cadrage]
                            lignes_visages.append(ligne)
                    except Exception as e:  # noqa: BLE001
                        lignes_visages.append(["(erreur)", chemin_rel, date, ""] + [""] * 10 + [f"erreur : {e}"])
                if favori:
                    favoris.append((chemin_rel, date, "; ".join(personnes)))
                if statut != "(simulation)":
                    etat[cle] = {"sig": sig, "row": ligne_cat, "faces": visages_bruts,
                                 "visages": lignes_visages[idx_visages:], "personnes": personnes}
                elif prec:
                    etat[cle] = prec

                if n_total % 200 == 0:
                    print(f"  {n_total} fichiers vus, {n_modif} retouchés, {n_erreurs} erreurs…")
                if time.time() - derniere_sauvegarde > 600:      # sauvegarde de l'état toutes les 10 minutes
                    sauver_etat()
                    derniere_sauvegarde = time.time()
    except StopIteration:
        pass
    except KeyboardInterrupt:
        interrompu = True
        print("\nInterruption demandée (Ctrl+C) : enregistrement de l'état et du catalogue… ne fermez pas la fenêtre.")

    # ---- fichiers connus de l'extraction précédente mais non vus cette fois (interruption, échantillon…)
    #      les sources supprimées depuis disparaissent du catalogue (sauf en cas d'interruption)
    for cle_p, e in etat_prec.items():
        if cle_p not in etat and e.get("row"):
            if interrompu or a.echantillon or os.path.exists(os.path.join(src_root, *cle_p.split("/"))):
                etat[cle_p] = e
                lignes.append(e["row"])
                lignes_visages.extend(e.get("visages", []))

    # ---- catalogue
    if not a.simulation:
        os.makedirs(cat, exist_ok=True)
        ecrire_csv(os.path.join(cat, "catalogue_photos.csv"),
                   ["Chemin", "Date", "Favori", "Personnes", "Retouchée", "Original sauvegardé par Picasa",
                    "Retouches appliquées", "Rendu approché", "Retouches non reproduites", "Légende", "Statut",
                    "Dernière extraction", "Nombre de mises à jour", "Dernière mise à jour", "Intégrité du fichier source"],
                   lignes)
        abimees = [[l[0], l[14], l[10]] for l in lignes if len(l) > 14 and str(l[14]).startswith("ENDOMMAGÉE")]
        ecrire_csv(os.path.join(cat, "photos_endommagees.csv"), ["Chemin", "Diagnostic", "Statut"], abimees)
        lignes_visages.sort(key=lambda l: (l[0].lower(), l[1]))
        ecrire_csv(os.path.join(cat, "personnes.csv"),
                   ["Personne", "Photo", "Date", "Favori", "Gauche", "Haut", "Largeur", "Hauteur",
                    "Photo largeur px", "Photo hauteur px", "X px", "Y px", "Visage largeur px", "Visage hauteur px",
                    "Cadrage"],
                   lignes_visages)
        ecrire_csv(os.path.join(cat, "personnes_resume.csv"),
                   ["Personne", "Nombre de photos", "Photos"],
                   [[p, len(v), " | ".join(v)] for p, v in sorted(par_personne.items(), key=lambda x: -len(x[1]))])
        etat_prec = {}          # tout est déjà dans etat
        sauver_etat()
        ecrire_csv(os.path.join(cat, "favoris.csv"), ["Chemin", "Date", "Personnes"], favoris)
        with open(os.path.join(cat, "LISEZ-MOI.txt"), "w", encoding="utf-8") as f:
            f.write(APROPOS + "\n\nFichiers CSV : séparateur « ; », encodage UTF-8 (ouverture directe dans Excel).\n"
                    "\npersonnes.csv : une ligne par personne ET par photo. Gauche/Haut/Largeur/Hauteur = rectangle du "
                    "visage en fractions (0 à 1, point décimal) de la photo EXPORTÉE, orientation d'affichage comprise ; "
                    "les colonnes px donnent la même chose en pixels. Cadrage : entier / partiel / hors cadre "
                    "(visage coupé ou exclu par un recadrage ; pas de rectangle dans ce dernier cas). "
                    "personnes_resume.csv : une ligne par personne avec la liste de ses photos.\n"
                    "\netat_export.json : mémoire de l'extraction, utilisée pour ne refaire que ce qui a changé "
                    "à la relance. Le supprimer revient à tout refaire.\n")

    duree = time.time() - t0
    print("\n=== Interrompu (relancer pour reprendre) ===" if interrompu else "\n=== Terminé ===")
    print(f"Nouveaux : {n_nouveaux} | inchangés : {n_inchanges} | modifiés dans Picasa : {n_maj} | "
          f"refaits : {n_reprises} | seulement personnes/favori/légende : {n_catalogue}")
    print(f"Fichiers : {n_total} | retouchés : {n_modif} | personnes : {len(par_personne)} | "
          f"favoris : {len(favoris)} | erreurs : {n_erreurs} | durée : {duree:.0f} s")
    if not a.simulation:
        n_ab = sum(1 for l in lignes if len(l) > 14 and str(l[14]).startswith("ENDOMMAGÉE"))
        if n_ab:
            print(f"Photos dont le fichier source est abîmé : {n_ab} (liste : photos_endommagees.csv)")
        print(f"Catalogue : {cat}")
    if interrompu:
        return 3
    return 0 if n_erreurs == 0 else 2


def ecrire_csv(chemin, entetes, lignes):
    with open(chemin, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(entetes)
        w.writerows(lignes)


def main():
    p = argparse.ArgumentParser(
        description="Extrait les photos retouchées d'une photothèque Picasa vers une arborescence normale.")
    p.add_argument("source", nargs="?", help="dossier racine des photos (ex. D:\\Photos)")
    p.add_argument("sortie", nargs="?", help="dossier de destination (nouvelle arborescence)")
    p.add_argument("--sans-originaux", dest="originaux", action="store_false",
                   help="ne pas créer de sous-dossier avec les originaux")
    p.add_argument("--sous-dossier-originaux", default="_Originaux_Picasa", help="nom du sous-dossier des originaux")
    p.add_argument("--qualite", type=int, default=95, help="qualité JPEG des photos rendues (défaut 95)")
    p.add_argument("--contacts", help="chemin de contacts.xml de Picasa (détecté automatiquement sinon)")
    p.add_argument("--sans-exiftool", action="store_true", help="ne pas écrire personnes/favoris dans les photos")
    p.add_argument("--ecraser", action="store_true", help="tout refaire, sans tenir compte de la précédente extraction")
    p.add_argument("--echantillon", type=int, default=0, metavar="N",
                   help="ne traiter que les N premières photos retouchées (pour vérifier le rendu)")
    p.add_argument("--rejouer", action="store_true",
                   help="rejouer aussi les yeux rouges/retouches déjà enregistrés dans la photo du dossier (déconseillé)")
    p.add_argument("--sans-verification", dest="verifier", action="store_false",
                   help="ne pas contrôler l'intégrité des fichiers JPEG")
    p.add_argument("--sans-reconstruction", dest="reconstruire", action="store_false",
                   help="ne pas compléter les photos abîmées avec leur miniature intégrée")
    p.add_argument("--simulation", action="store_true", help="ne rien écrire, juste compter")
    p.add_argument("--apropos", action="store_true", help="afficher la version et la licence")
    a = p.parse_args()

    if a.apropos:
        print(APROPOS)
        return 0
    print(APROPOS + "\n")
    if not a.source:
        a.source = input("Dossier source (photos Picasa) : ").strip().strip('"')
    if not a.sortie:
        a.sortie = input("Dossier de destination        : ").strip().strip('"')
    return executer(a)


if __name__ == "__main__":
    sys.exit(main())
