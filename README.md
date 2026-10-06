# PicasaExport v1.0.0 — CGExcel

Extrait d'une photothèque **Picasa** (retouches invisibles hors de Picasa) une arborescence ordinaire :
mêmes dossiers, photos retouchées, originaux en sous-dossier, dates conservées, catalogue des personnes et des favoris.

© 2026 Cyrille Gindre — CGExcel — Licence MIT + BAL 1.0 (Bonne Action License), voir `LICENSE`.

En échange, une seule chose vous est demandée, sur l'honneur : faire une bonne action chaque jour. Aider un voisin, sourire à un inconnu, ramasser un papier… c'est vous qui voyez.

## Installation

1. Installer Python 3.8+ (python.org, cocher « Add to PATH »).
2. Placer `picasa_export.py` et `lancer_export.bat` dans un même dossier.
3. Double-cliquer sur `lancer_export.bat` (installe Pillow et numpy au premier lancement), puis répondre aux deux questions : dossier source, dossier de destination.

En ligne de commande : `python picasa_export.py D:\Photos E:\Photos_export`

**Conseil : fermer Picasa pendant l'export, et commencer par un essai :**
`python picasa_export.py D:\Photos E:\Test --echantillon 20`

## Ce que fait l'outil

Pour chaque dossier (ex. `2026\12\toulouse`), il lit le fichier caché `.picasa.ini` et le dossier caché `.picasaoriginals`.

| Cas | Résultat dans la destination |
|---|---|
| Photo non retouchée | copie identique (dates et métadonnées conservées) |
| Retouche encore « virtuelle » (seulement dans `.picasa.ini`) | photo rendue avec ses retouches + original dans `Originaux_Picasa\` |
| Retouche déjà enregistrée par Picasa (original dans `.picasaoriginals`) | photo retouchée copiée + original dans `Originaux_Picasa\` |
| Autres fichiers (vidéos…) | copiés |

Les dates (modification, création Windows) et les données EXIF (dont la date de prise de vue) sont conservées.

## Catalogue (dossier `_Catalogue_Picasa`)

CSV prêts pour Excel (séparateur `;`, UTF-8) :

- `catalogue_photos.csv` : une ligne par fichier (date, favori, personnes, retouches appliquées, rendu approché, retouches non reproduites, statut) ;
- `personnes.csv` : chaque personne, son nombre de photos et la liste ;
- `favoris.csv` : photos marquées d'une étoile.

Si **exiftool** est installé (dans le PATH), les noms des personnes et les favoris sont aussi écrits dans les photos (XMP : personnes, mots-clés, note 5 étoiles + mot-clé « Favori »), sans changer la date du fichier. Option `--sans-exiftool` pour l'éviter.

## Limites à connaître

- Les noms viennent de la section `[Contacts2]` des `.picasa.ini` et, à défaut, de `contacts.xml` de Picasa (détecté automatiquement, ou `--contacts chemin`).
- Les algorithmes d'éclairage de Picasa ne sont pas publics. Reproduits **exactement** : recadrage, rotation. **Approchés** : lumière d'appoint (`fill`), éclairage/couleur automatiques, noir et blanc, sépia. **Non reproduits** (signalés dans le catalogue, colonne « Retouches non reproduites ») : redressement (`tilt`), effets, réglages fins… La photo originale reste toujours disponible dans `Originaux_Picasa\`.
- Seul le dossier `.picasaoriginals` (Picasa 3) est reconnu pour les sauvegardes d'originaux.

## Options

`--sans-originaux`, `--sous-dossier-originaux NOM`, `--qualite 95`, `--ecraser`, `--echantillon N`, `--simulation`, `--contacts CHEMIN`, `--sans-exiftool`, `--apropos`.
Sans `--ecraser`, les fichiers déjà exportés sont ignorés : on peut relancer après une interruption.
