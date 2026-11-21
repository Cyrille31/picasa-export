# PicasaExport v1.2.0 — CGExcel

Extrait d'une photothèque **Picasa** (retouches invisibles hors de Picasa) une arborescence ordinaire :
mêmes dossiers, photos retouchées, originaux en sous-dossier, dates conservées, catalogue des personnes et des favoris.

© 2026 Cyrille Gindre — CGExcel — Licence MIT + BAL 1.0 (Bonne Action License), voir `LICENSE`.

En échange, une seule chose vous est demandée, sur l'honneur : faire une bonne action chaque jour. Aider un voisin, sourire à un inconnu, ramasser un papier… c'est vous qui voyez.

## Installation

1. Installer Python 3.8+ (python.org, cocher « Add to PATH »).
2. Placer `picasa_export.py` et `lancer_export.bat` dans un même dossier.
3. Placer ces deux fichiers **en dehors** du dossier des photos (par exemple dans le dossier qui contient `photo` et `photo new`) : tout fichier présent dans la source est copié.
4. Double-cliquer sur `lancer_export.bat` (installe Pillow et numpy au premier lancement). Trois fenêtres s'ouvrent l'une après l'autre :
   1. **le dossier des photos** (ex. `photo`) ;
   2. **la liste de ses sous-dossiers** : sélectionner ceux à exporter (Ctrl+clic pour en ajouter, Maj+clic pour une plage, ex. 2021 à 2025 ; bouton « Tout sélectionner ») — tout leur contenu, sous-dossiers compris, est traité ;
   3. **le dossier de destination** (ex. `photo new`) : `photo new\2021`, `photo new\2022`… sont créés s'ils manquent, complétés s'ils existent.

   Un récapitulatif demande confirmation, puis l'export se déroule dans la fenêtre noire. Le catalogue reste unique (`photo new\_Catalogue_Picasa`) : les lignes des dossiers non sélectionnés y sont conservées.

En ligne de commande : `python picasa_export.py D:\Photos E:\Photos_export` (tout le dossier), ou seulement certains sous-dossiers :
`python picasa_export.py "D:\photo" "D:\photo new" --dossiers "2021;2022;2023;2024;2025"`.
`--console` pose les questions dans la fenêtre au lieu d'ouvrir des fenêtres de choix.

**Conseil : fermer Picasa pendant l'export, et commencer par un essai :**
`python picasa_export.py D:\Photos E:\Test --echantillon 20`

## Ce que fait l'outil

Pour chaque dossier (ex. `2026\12\toulouse`), il lit le fichier caché `.picasa.ini` et le dossier caché `.picasaoriginals`.

| Cas | Résultat dans la destination |
|---|---|
| Photo non retouchée | copie identique (dates et métadonnées conservées) |
| Retouche encore « virtuelle » (seulement dans `.picasa.ini`) | photo rendue avec ses retouches + original dans `_Originaux_Picasa\` |
| Retouche déjà enregistrée par Picasa (original dans `.picasaoriginals`, yeux rouges compris) | la photo du dossier contient déjà les modifications « cuites » (yeux rouges, retouches) : celles-ci ne sont pas rejouées, mais tous les autres réglages du `.picasa.ini` (recadrage, redressement, effets, rotation…) sont rejoués sur la photo du dossier (le `.picasa.ini` du dossier ne garde que ce qui est resté virtuel ; ce qui a été cuit — recadrage, redressement, rotation, auto-éclaircissement… — n'y figure plus) + original (celui de `.picasaoriginals`) dans `_Originaux_Picasa\` |
| Autres fichiers (vidéos…) | copiés |

Les dates (modification, création Windows) et les données EXIF (dont la date de prise de vue) sont conservées.

## Photos abîmées

Avant de copier un JPEG, le programme contrôle sa structure (lecture du fichier, sans décodage, ~0,05 s par photo) : marqueurs impossibles dans les données compressées, zones vides, fin de fichier absente. Une photo abîmée est signalée dans la colonne « Intégrité du fichier source » du catalogue et listée dans `photos_endommagees.csv`.

Si le fichier contient sa miniature intégrée (cas des photos de téléphone, 512×384), la partie illisible, en bas de l'image, est complétée avec cette miniature agrandie (fondu sur 24 lignes) ; la partie lisible garde sa qualité d'origine. Le fichier abîmé est conservé dans `_Originaux_Picasa`. Attention : la partie reconstruite est floue, ce n'est pas une vraie réparation. Sans miniature, la photo est copiée telle quelle et signalée.

Options : `--sans-verification` (ne pas contrôler les JPEG), `--sans-reconstruction` (signaler sans reconstruire).

## Relance incrémentale (photos modifiées dans Picasa)

On peut relancer le programme sur la même destination : seul ce qui a changé est refait. Il suffit pour cela de garder `_Catalogue_Picasa\etat_export.json`, la mémoire de l'extraction. Le supprimer revient à tout refaire.

Quand on modifie une photo dans Picasa, seul le `.picasa.ini` du dossier change, mais ce fichier contient **une section par photo** (`[IMG_1234.jpg]`). Le programme mémorise pour chaque photo une empreinte de sa section, de la taille et de la date du fichier et de l'original sauvegardé. Il sait donc exactement quelle photo a été modifiée. Cette vérification ne relit aucune photo et ne fait qu'une lecture par dossier, ce qui compte sur un disque externe lent.

| Ce qui a changé depuis la dernière extraction | Ce que fait la relance |
|---|---|
| Rien (même si le `.picasa.ini` a été réenregistré) | rien |
| Retouches de la photo (recadrage, filtres, rotation…) ou la photo elle-même | la photo est refaite ; statut complété par « mise à jour le … » |
| Seulement personnes, étoile ou légende | catalogue mis à jour, la photo n'est pas refaite (exiftool réécrit les noms et l'étoile s'il est installé) |
| Nouvelle photo | exportée |
| Photo supprimée de la photothèque | retirée du catalogue (le fichier exporté n'est pas effacé) |
| Fichier exporté effacé ou photo en erreur la fois précédente | refait |

**Interruption :** `Ctrl+C` dans la fenêtre arrête proprement le programme. L'état et le catalogue sont enregistrés, et la relance reprend où il s'était arrêté. L'état est aussi sauvegardé toutes les 10 minutes, en cas de coupure brutale.

`--ecraser` refait tout.

### Contrôle de la sortie (le catalogue n'est pas cru sur parole)

Une photo n'est considérée comme faite que si **le fichier exporté est réellement présent et est bien celui qui a été écrit**. À l'écriture, le programme relève sa taille, sa date et sa **somme de contrôle SHA-256** (colonnes du catalogue). À chaque relance :

| Constat dans la destination | Ce que fait la relance |
|---|---|
| photo exportée absente | refaite (« fichier absent de la sortie ») |
| taille ou date différente de celles relevées (fichier remplacé, copié d'ailleurs…) | refaite |
| original absent de `_Originaux_Picasa` (photo retouchée) | refaite |
| somme de contrôle différente (seulement avec `--controle-complet`, qui relit toutes les photos : plus long) | refaite |
| extraction faite avec une version antérieure (sans relevé) | une simple copie est vérifiée par sa taille ; une photo retouchée est refaite une fois |

La ligne du catalogue est alors mise à jour, avec le motif dans le statut (« refaite : … »), et la fin du traitement affiche le nombre de photos refaites par motif.
Colonnes utiles : **« Dernière extraction »** = date à laquelle la photo a été produite ; **« Dernière vérification dans la sortie »** = date de la dernière relance qui l'a contrôlée (une date ancienne signale une ligne d'un dossier non sélectionné depuis).

## Mémoire

Les grandes images sont traitées par bandes. Le programme mesure la mémoire libre au lancement et découpe davantage si elle manque. C'est utile sur un vieil ordinateur : avec moins de 1,5 Go libres, les images de plus de 2 Mpx sont traitées par bandes. Une photo qui n'a pu être lue est copiée telle quelle et signalée dans le catalogue.

## Catalogue (dossier `_Catalogue_Picasa`)

CSV prêts pour Excel (séparateur `;`, UTF-8) :

- `catalogue_photos.csv` : une ligne par fichier (date, favori, personnes, retouches appliquées, rendu approché, retouches non reproduites, statut, date d'extraction, taille et somme de contrôle SHA-256 du fichier exporté, date de dernière vérification) ;
- `personnes.csv` : **une ligne par personne et par photo**, avec la position du visage (rectangle en fractions et en pixels, dans la photo exportée : recadrage, redressement et rotation pris en compte) et son cadrage (entier / partiel / hors cadre) ;
- `personnes_resume.csv` : une ligne par personne (nombre de photos et liste) ;
- `favoris.csv` : photos marquées d'une étoile.

Si **exiftool** est installé (dans le PATH), les noms des personnes et les favoris sont aussi écrits dans les photos (XMP : personnes, mots-clés, note 5 étoiles + mot-clé « Favori »), sans changer la date du fichier. Option `--sans-exiftool` pour l'éviter.

## Limites à connaître

- Les noms viennent de la section `[Contacts2]` des `.picasa.ini` et, à défaut, de `contacts.xml` de Picasa (détecté automatiquement, ou `--contacts chemin`). Les entrées de `[Contacts]` qui ne sont que des liens vers un compte Google (`…_lh,…`) ne sont pas des noms : l'outil suit ce lien jusqu'à `contacts.xml` pour retrouver le vrai nom.
- Les algorithmes de Picasa ne sont pas publics : chaque réglage a été calé sur un panneau de photos de test (une photo par réglage, avant/après). Le tableau donne l'écart moyen avec Picasa, en niveaux sur 255 (le bruit JPEG seul fait ~1).

| Réglage Picasa | Statut | Écart |
|---|---|---|
| Recadrage, rotation, redressement (`tilt`), recadrage + rotation | **exact** | ~1-2 |
| Hautes lumières, ombres (`finetune2` 2 et 3) | calé, quasi exact | 1-2 |
| Retouche rapide des couleurs (`finetune2` 4) | calé | ~1,5 |
| Température des couleurs (`finetune2` 5) | calé sur 2 valeurs | ~1,5 |
| Éclairage d'appoint (`finetune2` 1) | approché | 4-7 |
| Sépia, N&B, N&B filtré (`ansel`), inverse des couleurs, réchauffer, saturation négative | calé / exact | 0,2-2 |
| Netteté (`unsharp2`) | approché | 1,6-3 |
| « J'ai de la chance » (`enhance`) : étirement des niveaux canal par canal, bornes mêlant 70 % de statistique du canal et 30 % de statistique commune aux trois canaux (règle retrouvée sur 40 paires avant/après) | calé | médiane ~2, moyenne ~3 |
| Saturation positive, éclairage/couleur automatiques | approché | 2-7 |
| Néon (`Neon`) | approché (contours colorés sur fond noir, couleur du `.picasa.ini` prise en compte ; calé sur un seul export) | ~5 |
| Autres effets (grain, coloration, flou artistique, halo, N&B partiel, dégradé, infrarouge, Lomo, Holga, HDR, cinémascope, Orton, années 60, points chauds, traitements croisés, postérisation, deux tons, Boost, adoucir, vignettage, pixeliser, zoom focal, esquisse, néon, bande dessinée, bordure, ombre projetée, cadre musée, polaroïd) | **non reproduits** : signalés dans le catalogue, l'original reste dans `_Originaux_Picasa\` | |
| Yeux rouges (`redeye`) | détection automatique des pupilles rouges (dans le visage repéré s'il existe, sinon il faut une paire d'yeux) ; écart ~4 dans les pupilles. Picasa « cuit » la correction dans le fichier (original dans `.picasaoriginals`) : dans ce cas la correction n'est pas rejouée (les autres réglages le sont) | 4 |

- Les recadrages de Picasa sont exprimés dans le repère brut du fichier, avant l'orientation EXIF (photos prises en portrait) : l'outil applique donc les retouches dans ce repère, puis l'orientation. Vérifié sur une photo portrait ; le cas d'une rotation faite dans Picasa (`rotate(n)`) combinée à un recadrage reste à confirmer. La photo originale reste toujours disponible dans `_Originaux_Picasa\`.
- Seul le dossier `.picasaoriginals` (Picasa 3) est reconnu pour les sauvegardes d'originaux.

## Options

`--sans-originaux`, `--sous-dossier-originaux NOM`, `--rejouer` (rejoue aussi les yeux rouges/retouches déjà cuits : déconseillé), `--qualite 95`, `--ecraser`, `--echantillon N`, `--simulation`, `--contacts CHEMIN`, `--sans-exiftool`, `--sans-verification`, `--sans-reconstruction`, `--dossiers "A;B"` (seulement ces sous-dossiers de la source), `--controle-complet` (vérifie la somme de contrôle de chaque photo déjà exportée), `--console` (questions dans la fenêtre plutôt que fenêtres de choix), `--apropos`.

Sans `--ecraser`, seul ce qui a changé est refait (voir « Relance incrémentale »).
