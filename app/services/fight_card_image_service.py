import io
from typing import Optional

import httpx
from PIL import Image, ImageDraw, ImageFont
from pymongo.asynchronous.database import AsyncDatabase

from app.services.s3_service import S3NotConfiguredError, get_s3_service

# Constantes de layout
CANVAS_WIDTH = 1200
PADDING = 40
GAP_X = 20
GAP_Y = 16
BLOCK_HEIGHT = 210
HEADER_HEIGHT = 120
SECTION_LABEL_HEIGHT = 50
FOOTER_HEIGHT = 40

# Colores
BG_COLOR = (10, 10, 10)
BLOCK_BG = (20, 20, 24)
BLOCK_BORDER = (40, 40, 48)
TEXT_WHITE = (255, 255, 255)
TEXT_MUTED = (140, 140, 150)
VS_COLOR = (100, 100, 110)
PICK_RED = (220, 38, 38)
PICK_BLUE = (37, 99, 235)
SECTION_LINE = (50, 50, 60)
WEIGHT_BG = (30, 30, 36)
HEADER_ACCENT = (255, 56, 56)


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for p in ["C:/Windows/Fonts/arial.ttf", "arial.ttf", "DejaVuSans.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _load_font_bold(size: int) -> ImageFont.FreeTypeFont:
    for p in ["C:/Windows/Fonts/arialbd.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return _load_font(size)


FONT_TITLE = _load_font_bold(28)
FONT_SUBTITLE = _load_font(16)
FONT_FIGHTER_NAME = _load_font_bold(15)
FONT_FIGHTER_NAME_SM = _load_font_bold(12)
FONT_VS = _load_font_bold(18)
FONT_VS_SM = _load_font_bold(14)
FONT_WEIGHT = _load_font(12)
FONT_WEIGHT_SM = _load_font(10)
FONT_PICK_LABEL = _load_font_bold(11)
FONT_PICK_LABEL_SM = _load_font_bold(9)
FONT_SECTION = _load_font_bold(16)
FONT_INITIALS = _load_font_bold(36)
FONT_INITIALS_SM = _load_font_bold(24)

def _block_width_for_cols(cols: int) -> int:
    return (CANVAS_WIDTH - PADDING * 2 - GAP_X * (cols - 1)) // cols


async def _download_image(url: str, cache: dict) -> Optional[Image.Image]:
    if url in cache:
        return cache[url]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                img = Image.open(io.BytesIO(resp.content)).convert("RGB")
                cache[url] = img
                return img
    except Exception:
        pass
    cache[url] = None
    return None


def _crop_cover(img: Image.Image, tw: int, th: int) -> Image.Image:
    sw, sh = img.size
    scale = max(tw / sw, th / sh)
    nw, nh = int(sw * scale), int(sh * scale)
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - tw) // 2
    top = (nh - th) // 2
    return img.crop((left, top, left + tw, top + th))


def _make_placeholder(name: str, size: int) -> Image.Image:
    img = Image.new("RGB", (size, size), (50, 50, 58))
    draw = ImageDraw.Draw(img)
    parts = (name or "?").split()
    initials = parts[0][0].upper()
    if len(parts) > 1:
        initials += parts[-1][0].upper()
    font = FONT_INITIALS if size >= 100 else FONT_INITIALS_SM
    bbox = draw.textbbox((0, 0), initials, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(((size - tw) // 2, (size - th) // 2 - 4), initials, fill=TEXT_MUTED, font=font)
    return img


def _get_fighter_image_url(fighter: dict) -> Optional[str]:
    image_key = fighter.get("image_key")
    if image_key:
        try:
            s3 = get_s3_service()
            url = s3.get_cloudfront_url(image_key)
            if url:
                return url
        except S3NotConfiguredError:
            pass
    purl = fighter.get("profile_image_url", "")
    if purl and purl.startswith("http"):
        return purl
    return None


def _get_last_name(full_name: str, max_len: int = 14) -> str:
    parts = full_name.strip().split()
    if len(parts) <= 1:
        return full_name
    if len(full_name) <= max_len:
        return full_name
    return parts[-1]


def _draw_fight_block(
    canvas: Image.Image,
    draw: ImageDraw.Draw,
    x: int,
    y: int,
    block_w: int,
    bout: dict,
    picked_corner: Optional[str],
    downloaded_images: dict,
):
    """Dibuja un bloque de pelea en (x, y) con el ancho indicado."""
    # Escalar tamaño de imagen según el ancho del bloque
    if block_w >= 500:
        img_size = 120
        inner_pad = 20
        font_name = FONT_FIGHTER_NAME
        font_vs = FONT_VS
        font_wt = FONT_WEIGHT
        font_pick = FONT_PICK_LABEL
        max_name_len = 14
    elif block_w >= 320:
        img_size = 90
        inner_pad = 14
        font_name = FONT_FIGHTER_NAME_SM
        font_vs = FONT_VS_SM
        font_wt = FONT_WEIGHT_SM
        font_pick = FONT_PICK_LABEL_SM
        max_name_len = 12
    else:
        img_size = 70
        inner_pad = 10
        font_name = FONT_FIGHTER_NAME_SM
        font_vs = FONT_VS_SM
        font_wt = FONT_WEIGHT_SM
        font_pick = FONT_PICK_LABEL_SM
        max_name_len = 10

    # Fondo del bloque
    draw.rounded_rectangle(
        (x, y, x + block_w, y + BLOCK_HEIGHT),
        radius=8, fill=BLOCK_BG, outline=BLOCK_BORDER, width=1,
    )

    red_fighter = bout.get("fighters", {}).get("red", {})
    blue_fighter = bout.get("fighters", {}).get("blue", {})
    red_name = red_fighter.get("fighter_name", "TBD")
    blue_name = blue_fighter.get("fighter_name", "TBD")

    # Imágenes de peleadores
    img_y = y + 16
    left_img_x = x + inner_pad
    right_img_x = x + block_w - inner_pad - img_size

    # Peleador rojo (izquierda)
    red_img = downloaded_images.get(f"red_{bout.get('id')}")
    red_cropped = _crop_cover(red_img, img_size, img_size) if red_img else _make_placeholder(red_name, img_size)
    canvas.paste(red_cropped, (left_img_x, img_y))

    # Peleador azul (derecha)
    blue_img = downloaded_images.get(f"blue_{bout.get('id')}")
    blue_cropped = _crop_cover(blue_img, img_size, img_size) if blue_img else _make_placeholder(blue_name, img_size)
    canvas.paste(blue_cropped, (right_img_x, img_y))

    # Resaltado del pick
    if picked_corner == "red":
        draw.rectangle(
            (left_img_x - 3, img_y - 3, left_img_x + img_size + 3, img_y + img_size + 3),
            outline=PICK_RED, width=4,
        )
        label = "PICK"
        lbbox = draw.textbbox((0, 0), label, font=font_pick)
        lw = lbbox[2] - lbbox[0]
        draw.text((left_img_x + (img_size - lw) // 2, img_y + img_size + 5), label, fill=PICK_RED, font=font_pick)
    elif picked_corner == "blue":
        draw.rectangle(
            (right_img_x - 3, img_y - 3, right_img_x + img_size + 3, img_y + img_size + 3),
            outline=PICK_BLUE, width=4,
        )
        label = "PICK"
        lbbox = draw.textbbox((0, 0), label, font=font_pick)
        lw = lbbox[2] - lbbox[0]
        draw.text((right_img_x + (img_size - lw) // 2, img_y + img_size + 5), label, fill=PICK_BLUE, font=font_pick)

    # Texto VS
    vs_bbox = draw.textbbox((0, 0), "VS", font=font_vs)
    vs_w = vs_bbox[2] - vs_bbox[0]
    draw.text(
        (x + block_w // 2 - vs_w // 2, img_y + img_size // 2 - 10),
        "VS", fill=VS_COLOR, font=font_vs,
    )

    # Nombres de peleadores
    name_y = img_y + img_size + 20

    red_display = _get_last_name(red_name, max_name_len).upper()
    rb = draw.textbbox((0, 0), red_display, font=font_name)
    rw = rb[2] - rb[0]
    draw.text((left_img_x + (img_size - rw) // 2, name_y), red_display, fill=TEXT_WHITE, font=font_name)

    vs_sb = draw.textbbox((0, 0), "vs", font=FONT_WEIGHT_SM)
    vsw = vs_sb[2] - vs_sb[0]
    draw.text((x + block_w // 2 - vsw // 2, name_y + 2), "vs", fill=TEXT_MUTED, font=FONT_WEIGHT_SM)

    blue_display = _get_last_name(blue_name, max_name_len).upper()
    bb = draw.textbbox((0, 0), blue_display, font=font_name)
    bw = bb[2] - bb[0]
    draw.text((right_img_x + (img_size - bw) // 2, name_y), blue_display, fill=TEXT_WHITE, font=font_name)

    # Etiqueta de categoría de peso
    weight_y = y + BLOCK_HEIGHT - 22
    weight_text = (bout.get("weight_class") or "Unknown").upper() + " BOUT"
    if bout.get("is_title_fight"):
        weight_text = "TITLE: " + weight_text

    wt_bbox = draw.textbbox((0, 0), weight_text, font=font_wt)
    wt_w = wt_bbox[2] - wt_bbox[0]
    wt_x = x + (block_w - wt_w) // 2 - 6
    draw.rounded_rectangle(
        (wt_x, weight_y - 2, wt_x + wt_w + 12, weight_y + 14),
        radius=4, fill=WEIGHT_BG,
    )
    draw.text((wt_x + 6, weight_y), weight_text, fill=TEXT_MUTED, font=font_wt)


# Helpers para layout de filas

def _split_into_rows(bouts: list, cols_per_row: int) -> list[list]:
    """Divide una lista de peleas en filas de N elementos."""
    rows = []
    for i in range(0, len(bouts), cols_per_row):
        rows.append(bouts[i:i + cols_per_row])
    return rows


def _build_main_card_rows(main_card: list) -> list[tuple[list, int]]:
    """
    Construye el layout de filas para la cartelera principal.
    Devuelve una lista de (peleas_en_fila, columnas_de_esa_fila).

    Fila 1: Main + Co-Main (máximo 2 columnas)
    Filas restantes: 3 por fila, o 4 si quedan 4 o menos
    """
    rows = []

    # Fila 1: fila premium para main event y co-main.
    premium = main_card[:2]
    rows.append((premium, 2))

    # Peleas restantes de la cartelera principal
    rest = main_card[2:]
    if not rest:
        return rows

    # Si quedan 4 o menos, ponerlas todas en una sola fila
    if len(rest) <= 4:
        rows.append((rest, len(rest)))
    else:
        # Completar filas de 3
        for chunk in _split_into_rows(rest, 3):
            rows.append((chunk, 3))

    return rows


def _build_prelim_rows(prelims: list) -> list[tuple[list, int]]:
    """
    Construye el layout de filas para prelims: siempre 4 por fila.
    Devuelve una lista de (peleas_en_fila, columnas_de_esa_fila).
    """
    rows = []
    for chunk in _split_into_rows(prelims, 4):
        rows.append((chunk, 4))
    return rows


def _calc_rows_height(row_list: list[tuple]) -> int:
    """Altura total en píxeles para una lista de filas."""
    if not row_list:
        return 0
    return len(row_list) * (BLOCK_HEIGHT + GAP_Y)


def _draw_rows(
    canvas: Image.Image,
    draw: ImageDraw.Draw,
    start_y: int,
    row_list: list[tuple[list, int]],
    picks_map: dict,
    downloaded_images: dict,
) -> int:
    """
    Dibuja todas las filas comenzando en start_y.
    Devuelve la posición Y después de la última fila.
    """
    current_y = start_y
    for bouts_in_row, cols in row_list:
        bw = _block_width_for_cols(cols)
        for j, bout in enumerate(bouts_in_row):
            bx = PADDING + j * (bw + GAP_X)
            picked = picks_map.get(bout["id"])
            _draw_fight_block(canvas, draw, bx, current_y, bw, bout, picked, downloaded_images)
        current_y += BLOCK_HEIGHT + GAP_Y
    return current_y


# Punto de entrada principal

async def generate_fight_card_png(
    event: dict,
    db: AsyncDatabase,
    user_id: Optional[str] = None,
) -> bytes:
    # 1. Obtener peleas ordenadas por sección de cartelera.
    bouts_cursor = db["bouts"].find({"event_id": event["id"]}).sort([
        ("is_main_event", -1),
        ("is_co_main_event", -1),
        ("card_section", 1),
        ("card_order", 1),
    ])
    all_bouts = await bouts_cursor.to_list(length=None)

    # Combinar bout_details para enriquecer datos de imagen
    bout_ids = [b["id"] for b in all_bouts]
    details_cursor = db["bout_details"].find({"bout_id": {"$in": bout_ids}})
    details_list = await details_cursor.to_list(length=None)
    details_map = {d["bout_id"]: d for d in details_list}

    for bout in all_bouts:
        detail = details_map.get(bout["id"])
        if detail and "fighters" in detail:
            for corner in ["red", "blue"]:
                if corner in detail["fighters"]:
                    base = bout.get("fighters", {}).get(corner, {})
                    detailed = detail["fighters"][corner]
                    if base.get("image_key") and not detailed.get("image_key"):
                        detailed["image_key"] = base["image_key"]
                    if base.get("profile_image_url") and not detailed.get("profile_image_url"):
                        detailed["profile_image_url"] = base["profile_image_url"]
                    bout.setdefault("fighters", {})[corner] = detailed

    # Separar en cartelera principal y prelims
    main_card = [b for b in all_bouts if b.get("card_section") == "main"]
    prelims = [b for b in all_bouts if b.get("card_section") in ("prelim", "early_prelim")]
    if not main_card and not prelims:
        main_card = all_bouts

    # 2. Obtener picks del usuario.
    picks_map = {}
    if user_id:
        picks_cursor = db["picks"].find({
            "user_id": user_id,
            "event_id": event["id"],
        })
        picks_list = await picks_cursor.to_list(length=None)
        for pick in picks_list:
            bout_id = pick["bout_id"]
            bout = next((b for b in all_bouts if b["id"] == bout_id), None)
            if not bout:
                continue
            picked_name = pick.get("picked_fighter_name", "").lower().strip()
            red_name = bout.get("fighters", {}).get("red", {}).get("fighter_name", "").lower().strip()
            blue_name = bout.get("fighters", {}).get("blue", {}).get("fighter_name", "").lower().strip()
            if picked_name == red_name:
                picks_map[bout_id] = "red"
            elif picked_name == blue_name:
                picks_map[bout_id] = "blue"

    # 3. Descargar imágenes de peleadores.
    image_cache = {}
    downloaded_images = {}

    for bout in all_bouts:
        for corner in ["red", "blue"]:
            fighter = bout.get("fighters", {}).get(corner, {})
            url = _get_fighter_image_url(fighter)
            if url:
                img = await _download_image(url, image_cache)
                if img:
                    downloaded_images[f"{corner}_{bout['id']}"] = img

    # 4. Construir layouts de filas.
    main_rows = _build_main_card_rows(main_card)
    prelim_rows = _build_prelim_rows(prelims)

    main_h = _calc_rows_height(main_rows)
    prelim_h = _calc_rows_height(prelim_rows)
    separator_h = SECTION_LABEL_HEIGHT if prelims else 0

    canvas_height = HEADER_HEIGHT + main_h + separator_h + prelim_h + FOOTER_HEIGHT + PADDING
    canvas_height = max(canvas_height, 400)

    # 5. Crear el canvas.
    canvas = Image.new("RGB", (CANVAS_WIDTH, canvas_height), BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    # 6. Dibujar encabezado.
    event_name = event.get("name", "UFC EVENT").upper()
    event_subtitle = event.get("subtitle", "")
    event_date = str(event.get("date", ""))
    location = event.get("location", {})
    venue = (location or {}).get("venue", "")
    city = (location or {}).get("city", "")
    location_str = f"{venue}, {city}" if venue and city else venue or city or ""

    draw.rectangle((0, 0, CANVAS_WIDTH, 4), fill=HEADER_ACCENT)

    title_bbox = draw.textbbox((0, 0), event_name, font=FONT_TITLE)
    title_w = title_bbox[2] - title_bbox[0]
    draw.text(((CANVAS_WIDTH - title_w) // 2, 20), event_name, fill=TEXT_WHITE, font=FONT_TITLE)

    info_parts = []
    if event_subtitle:
        info_parts.append(event_subtitle.upper())
    if event_date:
        info_parts.append(event_date)
    if location_str:
        info_parts.append(location_str)
    info_text = "  //  ".join(info_parts)
    if info_text:
        info_bbox = draw.textbbox((0, 0), info_text, font=FONT_SUBTITLE)
        info_w = info_bbox[2] - info_bbox[0]
        draw.text(((CANVAS_WIDTH - info_w) // 2, 58), info_text, fill=TEXT_MUTED, font=FONT_SUBTITLE)

    picks_label = "MY PICKS"
    pl_bbox = draw.textbbox((0, 0), picks_label, font=FONT_SECTION)
    pl_w = pl_bbox[2] - pl_bbox[0]
    draw.text(((CANVAS_WIDTH - pl_w) // 2, 85), picks_label, fill=HEADER_ACCENT, font=FONT_SECTION)

    # 7. Dibujar filas de la cartelera principal.
    current_y = _draw_rows(canvas, draw, HEADER_HEIGHT, main_rows, picks_map, downloaded_images)

    # 8. Dibujar el separador de prelims.
    if prelims:
        sep_y = current_y + SECTION_LABEL_HEIGHT // 2
        draw.line((PADDING, sep_y, CANVAS_WIDTH - PADDING, sep_y), fill=SECTION_LINE, width=1)
        sep_text = "PRELIMS"
        sep_bbox = draw.textbbox((0, 0), sep_text, font=FONT_SECTION)
        sep_w = sep_bbox[2] - sep_bbox[0]
        label_x = (CANVAS_WIDTH - sep_w) // 2 - 12
        draw.rectangle((label_x, sep_y - 10, label_x + sep_w + 24, sep_y + 12), fill=BG_COLOR)
        draw.text(((CANVAS_WIDTH - sep_w) // 2, sep_y - 9), sep_text, fill=TEXT_MUTED, font=FONT_SECTION)
        current_y += SECTION_LABEL_HEIGHT

    # 9. Dibujar filas de prelims.
    _draw_rows(canvas, draw, current_y, prelim_rows, picks_map, downloaded_images)

    # 10. Dibujar pie de página.
    footer_y = canvas_height - FOOTER_HEIGHT
    footer_text = "UFC PICKS // Generated Fight Card"
    ft_bbox = draw.textbbox((0, 0), footer_text, font=FONT_WEIGHT)
    ft_w = ft_bbox[2] - ft_bbox[0]
    draw.text(((CANVAS_WIDTH - ft_w) // 2, footer_y + 10), footer_text, fill=(60, 60, 70), font=FONT_WEIGHT)

    # 11. Exportar el PNG.
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
