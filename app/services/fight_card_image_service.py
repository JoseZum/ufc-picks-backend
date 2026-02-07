"""
Fight Card Image Generator

Generates a PNG fight card poster showing all bouts for an event
with the user's picks highlighted. Layout is a 2-column grid
similar to UFC fight card posters.
"""

import io
from typing import Optional

import httpx
from PIL import Image, ImageDraw, ImageFont
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.s3_service import get_s3_service, S3NotConfiguredError


# ── Layout constants ──────────────────────────────────────────────
CANVAS_WIDTH = 1200
PADDING = 40
GAP_X = 30
GAP_Y = 20
COLUMNS = 2
BLOCK_WIDTH = (CANVAS_WIDTH - PADDING * 2 - GAP_X) // COLUMNS  # 545
BLOCK_HEIGHT = 210
FIGHTER_IMG_SIZE = 120
HEADER_HEIGHT = 120
SECTION_LABEL_HEIGHT = 50
FOOTER_HEIGHT = 40

# ── Colors ────────────────────────────────────────────────────────
BG_COLOR = (10, 10, 10)
BLOCK_BG = (20, 20, 24)
BLOCK_BORDER = (40, 40, 48)
TEXT_WHITE = (255, 255, 255)
TEXT_MUTED = (140, 140, 150)
TEXT_ACCENT = (200, 200, 210)
VS_COLOR = (100, 100, 110)
PICK_RED = (220, 38, 38)       # #dc2626
PICK_BLUE = (37, 99, 235)      # #2563eb
PICK_OVERLAY_ALPHA = 40
SECTION_LINE = (50, 50, 60)
WEIGHT_BG = (30, 30, 36)
HEADER_ACCENT = (255, 56, 56)


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Try to load a system font, fallback to default."""
    font_candidates = [
        "arial.ttf",
        "Arial.ttf",
        "arialbd.ttf",
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ]
    for font_path in font_candidates:
        try:
            return ImageFont.truetype(font_path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _load_font_bold(size: int) -> ImageFont.FreeTypeFont:
    """Try to load a bold system font, fallback to regular."""
    bold_candidates = [
        "arialbd.ttf",
        "Arial Bold.ttf",
        "DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ]
    for font_path in bold_candidates:
        try:
            return ImageFont.truetype(font_path, size)
        except (OSError, IOError):
            continue
    return _load_font(size)


# Pre-load fonts
FONT_TITLE = _load_font_bold(28)
FONT_SUBTITLE = _load_font(16)
FONT_FIGHTER_NAME = _load_font_bold(15)
FONT_VS = _load_font_bold(18)
FONT_WEIGHT = _load_font(12)
FONT_PICK_LABEL = _load_font_bold(11)
FONT_SECTION = _load_font_bold(16)
FONT_INITIALS = _load_font_bold(36)


async def _download_image(url: str, cache: dict) -> Optional[Image.Image]:
    """Download an image from URL with in-memory cache. Returns None on failure."""
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


def _crop_cover(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Resize and crop image to cover the target dimensions (center crop)."""
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w = int(src_w * scale)
    new_h = int(src_h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)

    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def _make_placeholder(name: str, size: int) -> Image.Image:
    """Generate a gray placeholder with fighter initials."""
    img = Image.new("RGB", (size, size), (50, 50, 58))
    draw = ImageDraw.Draw(img)

    # Get initials (first letter of first and last name)
    parts = (name or "?").split()
    initials = parts[0][0].upper()
    if len(parts) > 1:
        initials += parts[-1][0].upper()

    bbox = draw.textbbox((0, 0), initials, font=FONT_INITIALS)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(
        ((size - tw) // 2, (size - th) // 2 - 4),
        initials,
        fill=TEXT_MUTED,
        font=FONT_INITIALS,
    )
    return img


def _get_fighter_image_url(fighter: dict) -> Optional[str]:
    """Get the best available image URL for a fighter."""
    # Check image_key first (CloudFront)
    image_key = fighter.get("image_key")
    if image_key:
        try:
            s3 = get_s3_service()
            url = s3.get_cloudfront_url(image_key)
            if url:
                return url
        except S3NotConfiguredError:
            pass

    # Fallback to profile_image_url if it's an absolute URL
    purl = fighter.get("profile_image_url", "")
    if purl and (purl.startswith("http://") or purl.startswith("https://")):
        return purl

    return None


def _draw_rounded_rect(draw: ImageDraw.Draw, xy, radius: int, fill=None, outline=None, width=1):
    """Draw a rounded rectangle."""
    x0, y0, x1, y1 = xy
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def _draw_fight_block(
    canvas: Image.Image,
    draw: ImageDraw.Draw,
    x: int,
    y: int,
    bout: dict,
    picked_corner: Optional[str],
    image_cache: dict,
    downloaded_images: dict,
):
    """Draw a single fight block at position (x, y)."""
    # Block background
    _draw_rounded_rect(draw, (x, y, x + BLOCK_WIDTH, y + BLOCK_HEIGHT), radius=8, fill=BLOCK_BG, outline=BLOCK_BORDER, width=1)

    red_fighter = bout.get("fighters", {}).get("red", {})
    blue_fighter = bout.get("fighters", {}).get("blue", {})

    red_name = red_fighter.get("fighter_name", "TBD")
    blue_name = blue_fighter.get("fighter_name", "TBD")

    # ── Fighter images ────────────────────────────────────────
    img_y = y + 18
    left_img_x = x + 20
    right_img_x = x + BLOCK_WIDTH - 20 - FIGHTER_IMG_SIZE

    # Red fighter image (left)
    red_img_key = f"red_{bout.get('id')}"
    if red_img_key in downloaded_images:
        red_img = downloaded_images[red_img_key]
    else:
        red_img = None
    if red_img:
        red_cropped = _crop_cover(red_img, FIGHTER_IMG_SIZE, FIGHTER_IMG_SIZE)
    else:
        red_cropped = _make_placeholder(red_name, FIGHTER_IMG_SIZE)
    canvas.paste(red_cropped, (left_img_x, img_y))

    # Blue fighter image (right)
    blue_img_key = f"blue_{bout.get('id')}"
    if blue_img_key in downloaded_images:
        blue_img = downloaded_images[blue_img_key]
    else:
        blue_img = None
    if blue_img:
        blue_cropped = _crop_cover(blue_img, FIGHTER_IMG_SIZE, FIGHTER_IMG_SIZE)
    else:
        blue_cropped = _make_placeholder(blue_name, FIGHTER_IMG_SIZE)
    canvas.paste(blue_cropped, (right_img_x, img_y))

    # ── Pick highlight ────────────────────────────────────────
    if picked_corner == "red":
        # Red border around left fighter
        draw.rectangle(
            (left_img_x - 3, img_y - 3, left_img_x + FIGHTER_IMG_SIZE + 3, img_y + FIGHTER_IMG_SIZE + 3),
            outline=PICK_RED,
            width=4,
        )
        # "YOUR PICK" label
        label = "YOUR PICK"
        lbbox = draw.textbbox((0, 0), label, font=FONT_PICK_LABEL)
        lw = lbbox[2] - lbbox[0]
        label_x = left_img_x + (FIGHTER_IMG_SIZE - lw) // 2
        label_y = img_y + FIGHTER_IMG_SIZE + 6
        draw.text((label_x, label_y), label, fill=PICK_RED, font=FONT_PICK_LABEL)
    elif picked_corner == "blue":
        # Blue border around right fighter
        draw.rectangle(
            (right_img_x - 3, img_y - 3, right_img_x + FIGHTER_IMG_SIZE + 3, img_y + FIGHTER_IMG_SIZE + 3),
            outline=PICK_BLUE,
            width=4,
        )
        label = "YOUR PICK"
        lbbox = draw.textbbox((0, 0), label, font=FONT_PICK_LABEL)
        lw = lbbox[2] - lbbox[0]
        label_x = right_img_x + (FIGHTER_IMG_SIZE - lw) // 2
        label_y = img_y + FIGHTER_IMG_SIZE + 6
        draw.text((label_x, label_y), label, fill=PICK_BLUE, font=FONT_PICK_LABEL)

    # ── VS text ───────────────────────────────────────────────
    vs_text = "VS"
    vs_bbox = draw.textbbox((0, 0), vs_text, font=FONT_VS)
    vs_w = vs_bbox[2] - vs_bbox[0]
    vs_x = x + BLOCK_WIDTH // 2 - vs_w // 2
    vs_y = img_y + FIGHTER_IMG_SIZE // 2 - 10
    draw.text((vs_x, vs_y), vs_text, fill=VS_COLOR, font=FONT_VS)

    # ── Fighter names ─────────────────────────────────────────
    name_y = img_y + FIGHTER_IMG_SIZE + 22

    # Red name (left-aligned under image)
    red_display = _get_last_name(red_name).upper()
    red_bbox = draw.textbbox((0, 0), red_display, font=FONT_FIGHTER_NAME)
    red_w = red_bbox[2] - red_bbox[0]
    red_name_x = left_img_x + (FIGHTER_IMG_SIZE - red_w) // 2
    draw.text((red_name_x, name_y), red_display, fill=TEXT_WHITE, font=FONT_FIGHTER_NAME)

    # "vs" between names
    vs_small = "vs"
    vs_small_bbox = draw.textbbox((0, 0), vs_small, font=FONT_WEIGHT)
    vs_small_w = vs_small_bbox[2] - vs_small_bbox[0]
    draw.text(
        (x + BLOCK_WIDTH // 2 - vs_small_w // 2, name_y + 2),
        vs_small,
        fill=TEXT_MUTED,
        font=FONT_WEIGHT,
    )

    # Blue name (centered under image)
    blue_display = _get_last_name(blue_name).upper()
    blue_bbox = draw.textbbox((0, 0), blue_display, font=FONT_FIGHTER_NAME)
    blue_w = blue_bbox[2] - blue_bbox[0]
    blue_name_x = right_img_x + (FIGHTER_IMG_SIZE - blue_w) // 2
    draw.text((blue_name_x, name_y), blue_display, fill=TEXT_WHITE, font=FONT_FIGHTER_NAME)

    # ── Weight class label ────────────────────────────────────
    weight_y = y + BLOCK_HEIGHT - 24
    weight_text = (bout.get("weight_class") or "Unknown").upper() + " BOUT"
    if bout.get("is_title_fight"):
        weight_text = "TITLE: " + weight_text

    wt_bbox = draw.textbbox((0, 0), weight_text, font=FONT_WEIGHT)
    wt_w = wt_bbox[2] - wt_bbox[0]

    # Centered weight class label with background
    wt_x = x + (BLOCK_WIDTH - wt_w) // 2 - 8
    draw.rounded_rectangle(
        (wt_x, weight_y - 2, wt_x + wt_w + 16, weight_y + 16),
        radius=4,
        fill=WEIGHT_BG,
    )
    draw.text((wt_x + 8, weight_y), weight_text, fill=TEXT_MUTED, font=FONT_WEIGHT)


def _get_last_name(full_name: str) -> str:
    """Extract the last name from a full name, or return the full name if short."""
    parts = full_name.strip().split()
    if len(parts) <= 1:
        return full_name
    # If the name is short enough, return full name
    if len(full_name) <= 14:
        return full_name
    return parts[-1]


async def generate_fight_card_png(
    event: dict,
    db: AsyncIOMotorDatabase,
    user_id: Optional[str] = None,
) -> bytes:
    """
    Generate a fight card PNG for an event, optionally with user picks highlighted.

    Args:
        event: Event document from MongoDB
        db: Database connection
        user_id: Optional user ID to highlight their picks

    Returns:
        PNG image bytes
    """
    # ── 1. Fetch bouts ordered by card section ────────────────
    bouts_cursor = db["bouts"].find({"event_id": event["id"]}).sort([
        ("is_main_event", -1),
        ("is_co_main_event", -1),
        ("card_section", 1),
        ("card_order", 1),
    ])
    all_bouts = await bouts_cursor.to_list(length=None)

    # Merge bout_details for image data
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
                    # Merge image_key from bouts if missing in details
                    if base.get("image_key") and not detailed.get("image_key"):
                        detailed["image_key"] = base["image_key"]
                    if base.get("profile_image_url") and not detailed.get("profile_image_url"):
                        detailed["profile_image_url"] = base["profile_image_url"]
                    # Use detailed data (has more fields)
                    bout.setdefault("fighters", {})[corner] = detailed

    # Separate into main card and prelims
    main_card = [b for b in all_bouts if b.get("card_section") == "main"]
    prelims = [b for b in all_bouts if b.get("card_section") in ("prelim", "early_prelim")]

    # If no card_section data, treat all as main card
    if not main_card and not prelims:
        main_card = all_bouts

    # ── 2. Fetch user picks ───────────────────────────────────
    picks_map = {}  # bout_id -> picked_corner
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

    # ── 3. Download all fighter images in parallel ────────────
    image_cache = {}
    downloaded_images = {}

    async def _download_fighter(bout_doc, corner):
        fighter = bout_doc.get("fighters", {}).get(corner, {})
        url = _get_fighter_image_url(fighter)
        if url:
            img = await _download_image(url, image_cache)
            if img:
                downloaded_images[f"{corner}_{bout_doc['id']}"] = img

    # Download all images (sequentially to avoid hammering CloudFront)
    for bout in all_bouts:
        await _download_fighter(bout, "red")
        await _download_fighter(bout, "blue")

    # ── 4. Calculate canvas height ────────────────────────────
    def _section_height(bouts_list):
        if not bouts_list:
            return 0
        rows = (len(bouts_list) + COLUMNS - 1) // COLUMNS
        return rows * (BLOCK_HEIGHT + GAP_Y)

    main_h = _section_height(main_card)
    prelim_h = _section_height(prelims)
    separator_h = SECTION_LABEL_HEIGHT if prelims else 0

    canvas_height = HEADER_HEIGHT + main_h + separator_h + prelim_h + FOOTER_HEIGHT + PADDING
    canvas_height = max(canvas_height, 400)

    # ── 5. Create canvas ──────────────────────────────────────
    canvas = Image.new("RGB", (CANVAS_WIDTH, canvas_height), BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    # ── 6. Draw header ────────────────────────────────────────
    event_name = event.get("name", "UFC EVENT").upper()
    event_subtitle = event.get("subtitle", "")
    event_date = str(event.get("date", ""))
    location = event.get("location", {})
    venue = location.get("venue", "") if location else ""
    city = location.get("city", "") if location else ""
    location_str = f"{venue}, {city}" if venue and city else venue or city or ""

    # Red accent line at top
    draw.rectangle((0, 0, CANVAS_WIDTH, 4), fill=HEADER_ACCENT)

    # Event name
    title_bbox = draw.textbbox((0, 0), event_name, font=FONT_TITLE)
    title_w = title_bbox[2] - title_bbox[0]
    draw.text(
        ((CANVAS_WIDTH - title_w) // 2, 20),
        event_name,
        fill=TEXT_WHITE,
        font=FONT_TITLE,
    )

    # Subtitle / date / location
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
        draw.text(
            ((CANVAS_WIDTH - info_w) // 2, 58),
            info_text,
            fill=TEXT_MUTED,
            font=FONT_SUBTITLE,
        )

    # "MY PICKS" label
    picks_label = "MY PICKS"
    pl_bbox = draw.textbbox((0, 0), picks_label, font=FONT_SECTION)
    pl_w = pl_bbox[2] - pl_bbox[0]
    draw.text(
        ((CANVAS_WIDTH - pl_w) // 2, 85),
        picks_label,
        fill=HEADER_ACCENT,
        font=FONT_SECTION,
    )

    # ── 7. Draw main card bouts ───────────────────────────────
    current_y = HEADER_HEIGHT

    for i, bout in enumerate(main_card):
        row = i // COLUMNS
        col = i % COLUMNS
        bx = PADDING + col * (BLOCK_WIDTH + GAP_X)
        by = current_y + row * (BLOCK_HEIGHT + GAP_Y)
        picked = picks_map.get(bout["id"])
        _draw_fight_block(canvas, draw, bx, by, bout, picked, image_cache, downloaded_images)

    current_y += main_h

    # ── 8. Draw prelims separator ─────────────────────────────
    if prelims:
        sep_y = current_y + SECTION_LABEL_HEIGHT // 2
        # Line
        draw.line(
            (PADDING, sep_y, CANVAS_WIDTH - PADDING, sep_y),
            fill=SECTION_LINE,
            width=1,
        )
        # Label
        sep_text = "PRELIMS"
        sep_bbox = draw.textbbox((0, 0), sep_text, font=FONT_SECTION)
        sep_w = sep_bbox[2] - sep_bbox[0]
        # Background for text
        label_x = (CANVAS_WIDTH - sep_w) // 2 - 12
        draw.rectangle(
            (label_x, sep_y - 10, label_x + sep_w + 24, sep_y + 12),
            fill=BG_COLOR,
        )
        draw.text(
            ((CANVAS_WIDTH - sep_w) // 2, sep_y - 9),
            sep_text,
            fill=TEXT_MUTED,
            font=FONT_SECTION,
        )
        current_y += SECTION_LABEL_HEIGHT

    # ── 9. Draw prelim bouts ──────────────────────────────────
    for i, bout in enumerate(prelims):
        row = i // COLUMNS
        col = i % COLUMNS
        bx = PADDING + col * (BLOCK_WIDTH + GAP_X)
        by = current_y + row * (BLOCK_HEIGHT + GAP_Y)
        picked = picks_map.get(bout["id"])
        _draw_fight_block(canvas, draw, bx, by, bout, picked, image_cache, downloaded_images)

    # ── 10. Footer ────────────────────────────────────────────
    footer_y = canvas_height - FOOTER_HEIGHT
    footer_text = "UFC PICKS // Generated Fight Card"
    ft_bbox = draw.textbbox((0, 0), footer_text, font=FONT_WEIGHT)
    ft_w = ft_bbox[2] - ft_bbox[0]
    draw.text(
        ((CANVAS_WIDTH - ft_w) // 2, footer_y + 10),
        footer_text,
        fill=(60, 60, 70),
        font=FONT_WEIGHT,
    )

    # ── 11. Export PNG ────────────────────────────────────────
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
