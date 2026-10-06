#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PicasaExport v1.0.0
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
        - originaux dans un sous-dossier (« Originaux_Picasa ») ;
        - dates et métadonnées (EXIF…) conservées ;
        - catalogue CSV : personnes reconnues, favoris, retouches.

Dépendances : Python 3.8+, Pillow, numpy   (pip install pillow numpy)
Optionnel   : exiftool dans le PATH, pour écrire personnes/favoris DANS les photos.
"""

import argparse
import csv
import ctypes
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime

VERSION = "1.0.0"

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

try:
    from PIL import Image, ImageOps
    import numpy as np
except ImportError:  # pragma: no cover
    print("Il manque Pillow et/ou numpy. Installez-les avec :  pip install pillow numpy")
    sys.exit(1)

Image.MAX_IMAGE_PIXELS = None


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
    sections, contacts = {}, {}
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
                contacts[cle.lower()] = val.split(";")[0].strip()
            elif courant.lower() != "picasa":
                sections[courant.lower()][cle.lower()] = val.strip()
        # on lit le premier fichier trouvé
        break
    return sections, contacts


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
    resultat = {}
    for c in candidats:
        if os.path.isfile(c):
            texte = lire_texte(c)
            for m in re.finditer(r'<contact\b[^>]*>', texte):
                balise = m.group(0)
                i = re.search(r'\bid="([^"]+)"', balise)
                n = re.search(r'\bname="([^"]*)"', balise)
                if i and n and n.group(1):
                    nom = (n.group(1).replace("&amp;", "&").replace("&quot;", '"')
                           .replace("&apos;", "'").replace("&lt;", "<").replace("&gt;", ">"))
                    resultat[i.group(1).lower()] = nom
            break
    return resultat


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


def f_fill(img, args):
    a = float(args[1]) if len(args) > 1 else 0.0
    arr = np.asarray(img, dtype=np.float32) / 255.0
    arr = arr + 0.5 * a * (1.0 - arr) ** 2
    return Image.fromarray((np.clip(arr, 0, 1) * 255 + 0.5).astype(np.uint8))


def f_bw(img, args):
    return img.convert("L").convert("RGB")


def f_sepia(img, args):
    g = img.convert("L")
    return ImageOps.colorize(g, black=(30, 15, 0), white=(255, 240, 205), mid=(150, 110, 70))


def f_autolight(img, args):
    return ImageOps.autocontrast(img, cutoff=0.5, preserve_tone=True)


def f_autocolor(img, args):
    return ImageOps.autocontrast(img, cutoff=0.5)


# nom -> (fonction, exact ?)  ; exact=False : rendu approché (algorithme Picasa non public)
FILTRES = {
    "crop64": (f_crop64, True),
    "bw": (f_bw, False),
    "sepia": (f_sepia, False),
    "fill": (f_fill, False),
    "autolight": (f_autolight, False),
    "autocolor": (f_autocolor, False),
}


def rendre(img, filtres, rotation):
    """Applique rotation puis filtres dans l'ordre. Retourne (img, appliqués, approchés, ignorés)."""
    appliques, approches, ignores = [], [], []
    img = vers_rgb(img)
    if rotation:
        img = img.rotate(-90 * rotation, expand=True)  # rotate(1) = 90° horaire
        appliques.append(f"rotate({rotation})")
    for nom, args in filtres:
        if args and args[0] == "0":
            continue  # filtre désactivé
        if nom not in FILTRES:
            ignores.append(nom)
            continue
        fonction, exact = FILTRES[nom]
        try:
            img = fonction(img, args)
            appliques.append(nom)
            if not exact:
                approches.append(nom)
        except Exception as e:  # noqa: BLE001
            ignores.append(f"{nom}(erreur: {e})")
    return img, appliques, approches, ignores


# --------------------------------------------------------------------------- fichiers & dates

def copier_dates(src, dst):
    st = os.stat(src)
    os.utime(dst, (st.st_atime, st.st_mtime))
    if os.name == "nt":
        try:
            _fixer_creation_windows(dst, st.st_ctime)
        except Exception:  # noqa: BLE001
            pass


def _fixer_creation_windows(chemin, ts):
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = wintypes.HANDLE
    h = k32.CreateFileW(chemin, 0x100, 0, None, 3, 0x80, None)  # FILE_WRITE_ATTRIBUTES
    if h in (-1, 0, None):
        return
    ft = int((ts + 11644473600) * 10000000)
    c = wintypes.FILETIME(ft & 0xFFFFFFFF, ft >> 32)
    k32.SetFileTime(h, ctypes.byref(c), None, None)
    k32.CloseHandle(h)


def date_prise_vue(chemin):
    try:
        with Image.open(chemin) as im:
            ex = im.getexif()
            v = ex.get_ifd(0x8769).get(0x9003) or ex.get(0x0132)
            if v:
                return datetime.strptime(str(v)[:19], "%Y:%m:%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        pass
    return datetime.fromtimestamp(os.path.getmtime(chemin)).strftime("%Y-%m-%d %H:%M:%S")


def trouver_sauvegarde(dossier, nom):
    rep = os.path.join(dossier, ".picasaoriginals")
    if not os.path.isdir(rep):
        return None
    exact = os.path.join(rep, nom)
    if os.path.isfile(exact):
        return exact
    base, ext = os.path.splitext(nom)
    candidats = sorted(f for f in os.listdir(rep)
                       if f.lower().startswith(base.lower()) and f.lower().endswith(ext.lower()))
    return os.path.join(rep, candidats[0]) if candidats else None


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


def rendre_vers(src, dst, filtres, rotation, qualite):
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)  # Picasa raisonne sur l'image « redressée »
        img, ap, apx, ig = rendre(im, filtres, rotation)
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

    exiftool = None if a.sans_exiftool else shutil.which("exiftool")
    glob_contacts = contacts_globaux(a.contacts)
    print(f"Source : {src_root}\nSortie : {dst_root}")
    print(f"exiftool : {'oui' if exiftool else 'non (personnes/favoris seulement dans le catalogue)'}")
    print(f"Noms de contacts globaux lus : {len(glob_contacts)}")
    if a.simulation:
        print("MODE SIMULATION : rien ne sera écrit.")

    lignes = []
    par_personne = defaultdict(list)
    favoris = []
    n_total = n_modif = n_erreurs = n_edit_traitees = 0
    t0 = time.time()

    for racine, dossiers, fichiers in os.walk(src_root):
        dossiers[:] = sorted(d for d in dossiers if d.lower() not in DOSSIERS_IGNORES)
        rel = os.path.relpath(racine, src_root)
        rel = "" if rel == "." else rel
        sections, contacts_loc = lire_ini(racine)
        contacts = dict(glob_contacts)
        contacts.update(contacts_loc)
        dest_dir = os.path.join(dst_root, rel)

        for nom in sorted(fichiers):
            if nom.lower() in FICHIERS_IGNORES:
                continue
            src = os.path.join(racine, nom)
            ext = os.path.splitext(nom)[1].lower()
            n_total += 1
            sec = sections.get(nom.lower(), {})
            est_image = ext in EXT_IMAGES
            filtres = parser_filtres(sec.get("filters")) if est_image else []
            rotation = parser_rotation(sec.get("rotate")) if est_image else 0
            if est_image:
                for nom_f, args in parser_filtres(sec.get("filters")):
                    if nom_f == "rotate":
                        rotation = (rotation + parser_rotation(f"rotate({args[0] if args else 0})")) % 4
                filtres = [f for f in filtres if f[0] != "rotate"]
            sauvegarde = trouver_sauvegarde(racine, nom) if est_image else None
            personnes = parser_visages(sec.get("faces"), contacts) if est_image else []
            favori = sec.get("star", "").lower() == "yes"
            legende = sec.get("caption", "")
            modifiee = bool(filtres or rotation or sauvegarde)
            if modifiee:
                n_modif += 1

            if a.echantillon and modifiee and n_edit_traitees >= a.echantillon:
                # mode échantillon : on ne traite plus rien après N photos retouchées
                print(f"Échantillon de {a.echantillon} photos retouchées atteint.")
                break

            dst = os.path.join(dest_dir, nom)
            statut, appliques, approches, ignores = "copiée", [], [], []
            try:
                a_rendre = bool(filtres or rotation)
                deja = os.path.exists(dst) and not a.ecraser
                if deja:
                    statut = "déjà présente (ignorée)"
                elif not a.simulation:
                    os.makedirs(dest_dir, exist_ok=True)
                    if a_rendre:
                        appliques, approches, ignores = rendre_vers(src, dst, filtres, rotation, a.qualite)
                        if appliques:
                            statut = "retouches rendues"
                        else:
                            shutil.copy2(src, dst)
                            statut = "copiée (aucune retouche applicable)"
                    else:
                        shutil.copy2(src, dst)
                        statut = "copiée (retouche déjà enregistrée)" if sauvegarde else "copiée"
                    if a.originaux and modifiee:
                        orig_src = sauvegarde or src
                        orig_dir = os.path.join(dest_dir, a.sous_dossier_originaux)
                        os.makedirs(orig_dir, exist_ok=True)
                        orig_dst = os.path.join(orig_dir, nom)
                        if a.ecraser or not os.path.exists(orig_dst):
                            shutil.copy2(orig_src, orig_dst)
                    if exiftool and est_image:
                        marquer_exiftool(exiftool, dst, personnes, favori)
                else:
                    if a_rendre:
                        _, appliques, approches, ignores = None, [f[0] for f in filtres if f[0] in FILTRES], \
                            [f[0] for f in filtres if f[0] in FILTRES and not FILTRES[f[0]][1]], \
                            [f[0] for f in filtres if f[0] not in FILTRES]
                        statut = "(simulation)"
                if modifiee:
                    n_edit_traitees += 1
            except Exception as e:  # noqa: BLE001
                n_erreurs += 1
                statut = f"ERREUR : {e}"

            date = date_prise_vue(src) if est_image else \
                datetime.fromtimestamp(os.path.getmtime(src)).strftime("%Y-%m-%d %H:%M:%S")
            chemin_rel = os.path.join(rel, nom) if rel else nom
            lignes.append([
                chemin_rel, date, "oui" if favori else "non", "; ".join(personnes),
                "oui" if modifiee else "non", "oui" if sauvegarde else "non",
                ", ".join(appliques), ", ".join(approches), ", ".join(ignores), legende, statut,
            ])
            for p in personnes:
                par_personne[p].append(chemin_rel)
            if favori:
                favoris.append((chemin_rel, date, "; ".join(personnes)))

            if n_total % 200 == 0:
                print(f"  {n_total} fichiers vus, {n_modif} retouchés, {n_erreurs} erreurs…")
        else:
            continue
        break  # sortie de l'os.walk en mode échantillon

    # ---- catalogue
    if not a.simulation:
        cat = os.path.join(dst_root, "_Catalogue_Picasa")
        os.makedirs(cat, exist_ok=True)
        ecrire_csv(os.path.join(cat, "catalogue_photos.csv"),
                   ["Chemin", "Date", "Favori", "Personnes", "Retouchée", "Original sauvegardé par Picasa",
                    "Retouches appliquées", "Rendu approché", "Retouches non reproduites", "Légende", "Statut"],
                   lignes)
        ecrire_csv(os.path.join(cat, "personnes.csv"),
                   ["Personne", "Nombre de photos", "Photos"],
                   [[p, len(v), " | ".join(v)] for p, v in sorted(par_personne.items(), key=lambda x: -len(x[1]))])
        ecrire_csv(os.path.join(cat, "favoris.csv"), ["Chemin", "Date", "Personnes"], favoris)
        with open(os.path.join(cat, "LISEZ-MOI.txt"), "w", encoding="utf-8") as f:
            f.write(APROPOS + "\n\nFichiers CSV : séparateur « ; », encodage UTF-8 (ouverture directe dans Excel).\n")

    duree = time.time() - t0
    print("\n=== Terminé ===")
    print(f"Fichiers : {n_total} | retouchés : {n_modif} | personnes : {len(par_personne)} | "
          f"favoris : {len(favoris)} | erreurs : {n_erreurs} | durée : {duree:.0f} s")
    if not a.simulation:
        print(f"Catalogue : {os.path.join(dst_root, '_Catalogue_Picasa')}")
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
    p.add_argument("--sous-dossier-originaux", default="Originaux_Picasa", help="nom du sous-dossier des originaux")
    p.add_argument("--qualite", type=int, default=95, help="qualité JPEG des photos rendues (défaut 95)")
    p.add_argument("--contacts", help="chemin de contacts.xml de Picasa (détecté automatiquement sinon)")
    p.add_argument("--sans-exiftool", action="store_true", help="ne pas écrire personnes/favoris dans les photos")
    p.add_argument("--ecraser", action="store_true", help="écraser les fichiers déjà présents en sortie")
    p.add_argument("--echantillon", type=int, default=0, metavar="N",
                   help="ne traiter que les N premières photos retouchées (pour vérifier le rendu)")
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
