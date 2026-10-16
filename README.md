# IKEA 3D Model Batch Downloader

This script batch downloads 3D model files (GLB format) from IKEA product pages, which you can open in 3D software like Blender.

This fork adds:

- **Country / language selection** – works with any IKEA site (`fr/fr`, `fi/en`, `de/de`, `us/en`, …)
- **Category listing** – fetches the full category tree of the chosen site and shows it as a numbered list
- **Category selection** – download one, several, or all categories
- **Variants on/off** – download every colour variant of each product, or only the main one
- **Command-line flags** for scripting, with an **interactive mode** as fallback when no arguments are given
- One shared browser instance for the whole run (much faster than the original one-browser-per-page)

<p align="left">
  <img src="https://raw.githubusercontent.com/apinanaivot/IKEA-3d-model-batch-downloader/main/sample.jpg" width="550" title="IKEA 3D Model Batch Downloader">
</p>
Sofa downloaded from the IKEA website rendered inside Blender

-----

NOTE: If you don't need to download entire catalogues of models at a time, check out the original author's other script that adds download buttons directly on the IKEA website: https://github.com/apinanaivot/IKEA-3D-Model-Download-Button

## Installation

1. Install Python 3.8 or later from [python.org](https://www.python.org/downloads/) (on Windows, tick "Add Python to PATH").
2. Install Google Chrome (the script drives a headless Chrome; the matching driver is downloaded automatically).
3. Download this repository and, from its folder, install the dependencies:
   ```
   pip install -r requirements.txt
   ```

## Usage

### Interactive mode

Run the script without arguments and follow the prompts:

```
python ikea-glb-downloader.py
```

You will be asked for:

1. The IKEA country code (e.g. `fr`, `fi`, `de`, `us`) and site language (e.g. `fr`, `en`)
2. Which categories to download – the script prints the full numbered category list; answer with numbers, ranges (`3-7`), category IDs (`fu002`), or `all`
3. Whether to download all colour variants

### Command line

```
# List the categories of the French site
python ikea-glb-downloader.py --country fr --lang fr --list-categories

# Download "Tables & chaises" and "Luminaires", with every colour variant
python ikea-glb-downloader.py -c fr -l fr --categories fu002,li001 --variants

# Download the entire catalogue, main variant only
python ikea-glb-downloader.py -c fi -l en --categories all --no-variants

# Original behaviour: a raw category URL
python ikea-glb-downloader.py --url https://www.ikea.com/fi/en/cat/sofas-fu003/ --variants
```

| Flag | Description |
|---|---|
| `-c`, `--country` | IKEA country code (`fr`, `fi`, `de`, `us`, …) |
| `-l`, `--lang` | Site language code; defaults to the country code |
| `--list-categories` | Print the category list and exit |
| `--categories` | What to download: category IDs or list numbers, comma separated, or `all` |
| `--url` | Download a raw category URL instead (can be repeated) |
| `--variants` / `--no-variants` | Download all colour variants, or only the main one (prompted if omitted in interactive mode, `--no-variants` otherwise) |
| `-o`, `--output` | Output directory (default `downloaded-files`) |
| `--db` | SQLite database path (default `ikea_products.db`) |
| `--max-pages` | Limit listing pages per category – handy for a quick test |
| `--refresh-categories` | Re-fetch the category list instead of using the cached `categories-<country>-<lang>.json` |
| `--no-headless` | Show the browser window |

Any flag you omit in a terminal is asked for interactively; when stdin is not a terminal (cron, CI), missing required values are an error instead.

Category IDs (`fu002`, `st001`, `li001`, `700640`, …) are the same on every IKEA country site; only the names change. Selecting a top-level category downloads all of its sub-categories.

## How it works

1. Fetches the site's "all products" page and parses it into categories and sub-categories (cached per country in `categories-<country>-<lang>.json`)
2. Walks every page of each selected category listing and collects product links
3. Opens each product page (and its colour variants if requested), reads the embedded 3D model URL and downloads the GLB
4. Records each product in an SQLite database so nothing is downloaded twice, even across runs and categories

Downloaded files are saved in the output directory as `NAME - Colour.glb`.

## Notes

- The script never downloads the same product twice; re-run it with other categories or countries to add more models. Databases created by the original script are upgraded automatically.
- Pagination stops when a listing page yields no new products.
- The CSS selectors used on the IKEA site are grouped at the top of the script (`SEL_*` constants) so they are easy to fix if IKEA changes its pages.
