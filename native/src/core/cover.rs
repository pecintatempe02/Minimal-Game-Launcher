//! Cover image storage and thumbnails.
//!
//! Selected images are copied into MGL's own `covers/` directory, so the
//! original file can be moved or deleted. A square thumbnail is generated for
//! library rendering so the full-size image is never decoded per frame.
//!
//! PNG is decoded natively. JPEG and WebP are decoded through a system image
//! converter when one is installed; if no decoder is available the import fails
//! with a clear message and the library is left unchanged.

use std::path::{Path, PathBuf};

use crate::core::paths;

/// Longest side of the library thumbnail, in pixels.
pub const THUMB_SIZE: u32 = 384;

/// Decoded RGBA pixels.
#[derive(Clone, Debug)]
pub struct CoverImage {
    pub width: u32,
    pub height: u32,
    pub rgba: Vec<u8>,
}

/// Paths written by a successful import.
#[derive(Clone, Debug)]
pub struct ImportedCover {
    pub cover: PathBuf,
    pub thumbnail: PathBuf,
    pub preview: CoverImage,
}

/// Import `source` for `game_id`: copy the original and write a square PNG
/// thumbnail. Existing managed files for the same game are replaced.
pub fn import(source: &Path, game_id: &str) -> Result<ImportedCover, String> {
    if !source.is_file() {
        return Err(format!("{} is not a file.", source.display()));
    }
    let bytes = std::fs::read(source)
        .map_err(|error| format!("Could not read {}: {error}", source.display()))?;
    let decoded = decode_bytes(&bytes, source)?;
    let square = square_thumbnail(&decoded, THUMB_SIZE);

    paths::ensure_dir(&paths::covers_dir()).map_err(|error| {
        format!(
            "Could not create {}: {error}",
            paths::covers_dir().display()
        )
    })?;

    // Remove a previous managed cover of this game first, including a cover in a
    // different format, so the games directory cannot accumulate orphans.
    remove_managed(None, None, Some(game_id));

    let extension = storage_extension(source, &bytes);
    let cover = paths::cover_path(game_id, extension);
    std::fs::write(&cover, &bytes)
        .map_err(|error| format!("Could not copy the cover to {}: {error}", cover.display()))?;
    let thumbnail = paths::thumbnail_path(game_id);
    write_png(&thumbnail, &square)?;

    Ok(ImportedCover {
        cover,
        thumbnail,
        preview: square,
    })
}

/// Load a small preview for an existing cover or thumbnail.
///
/// Used by the background loader at startup and by the editor. The image is
/// scaled to a square so the caller only ever deals with one size.
pub fn load_preview(path: &Path) -> Result<CoverImage, String> {
    let bytes = std::fs::read(path)
        .map_err(|error| format!("Could not read {}: {error}", path.display()))?;
    let decoded = decode_bytes(&bytes, path)?;
    Ok(square_thumbnail(&decoded, THUMB_SIZE))
}

/// Delete this game's managed cover and thumbnail. Paths outside the managed
/// covers directory are never touched.
pub fn remove_for_game(game_id: &str) {
    remove_managed(None, None, Some(game_id));
}

/// Delete explicitly stored paths, but only when they are managed covers.
pub fn remove_paths(cover: Option<&str>, thumbnail: Option<&str>) {
    remove_managed(cover, thumbnail, None);
}

fn remove_managed(cover: Option<&str>, thumbnail: Option<&str>, game_id: Option<&str>) {
    if let Some(path) = cover.filter(|path| paths::is_managed_cover(Path::new(path))) {
        let _ = std::fs::remove_file(path);
    }
    if let Some(path) = thumbnail.filter(|path| paths::is_managed_cover(Path::new(path))) {
        let _ = std::fs::remove_file(path);
    }
    if let Some(game_id) = game_id {
        let directory = paths::covers_dir();
        let Ok(entries) = std::fs::read_dir(&directory) else {
            return;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            let name = path
                .file_name()
                .map(|name| name.to_string_lossy().into_owned())
                .unwrap_or_default();
            if (name.starts_with(&format!("{game_id}.")) || name == format!("{game_id}.thumb.png"))
                && paths::is_managed_cover(&path)
            {
                let _ = std::fs::remove_file(path);
            }
        }
    }
}

fn storage_extension(source: &Path, bytes: &[u8]) -> &'static str {
    if bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
        return "png";
    }
    if bytes.starts_with(&[0xff, 0xd8, 0xff]) {
        return "jpg";
    }
    if bytes.len() > 12 && &bytes[0..4] == b"RIFF" && &bytes[8..12] == b"WEBP" {
        return "webp";
    }
    match source
        .extension()
        .map(|ext| ext.to_string_lossy().to_ascii_lowercase())
        .as_deref()
    {
        Some("jpg") | Some("jpeg") => "jpg",
        Some("webp") => "webp",
        Some("png") => "png",
        _ => "img",
    }
}

fn decode_bytes(bytes: &[u8], source: &Path) -> Result<CoverImage, String> {
    if bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
        return decode_png(bytes)
            .map_err(|error| format!("Could not decode {} as PNG: {error}", source.display()));
    }
    // JPEG/WebP: use a system converter to produce PNG, then decode that.
    let png = convert_to_png(source)?;
    decode_png(&png).map_err(|error| {
        format!(
            "Could not decode the converted image for {}: {error}",
            source.display()
        )
    })
}

fn decode_png(bytes: &[u8]) -> Result<CoverImage, String> {
    let mut decoder = png::Decoder::new(std::io::Cursor::new(bytes));
    // Expand palette images and low bit depths, and strip 16-bit samples, so
    // every PNG reaches `expand_rgba` as 8-bit RGB/RGBA/greyscale.
    decoder.set_transformations(png::Transformations::normalize_to_color8());
    let mut reader = decoder.read_info().map_err(|error| error.to_string())?;
    let size = reader.output_buffer_size();
    let mut buffer = vec![0u8; size];
    let info = reader
        .next_frame(&mut buffer)
        .map_err(|error| error.to_string())?;
    let rgba = expand_rgba(
        &buffer,
        info.width,
        info.height,
        info.color_type,
        info.bit_depth,
    )?;
    Ok(CoverImage {
        width: info.width,
        height: info.height,
        rgba,
    })
}

fn expand_rgba(
    data: &[u8],
    width: u32,
    height: u32,
    color: png::ColorType,
    depth: png::BitDepth,
) -> Result<Vec<u8>, String> {
    if depth != png::BitDepth::Eight {
        return Err(format!("unsupported PNG bit depth {depth:?}"));
    }
    let pixels = (width as usize) * (height as usize);
    let mut out = Vec::with_capacity(pixels * 4);
    match color {
        png::ColorType::Rgba => out.extend_from_slice(&data[..pixels * 4]),
        png::ColorType::Rgb => {
            for px in data[..pixels * 3].chunks(3) {
                out.extend_from_slice(&[px[0], px[1], px[2], 255]);
            }
        }
        png::ColorType::GrayscaleAlpha => {
            for px in data[..pixels * 2].chunks(2) {
                out.extend_from_slice(&[px[0], px[0], px[0], px[1]]);
            }
        }
        png::ColorType::Grayscale => {
            for value in &data[..pixels] {
                out.extend_from_slice(&[*value, *value, *value, 255]);
            }
        }
        png::ColorType::Indexed => {
            return Err("indexed PNG could not be expanded".to_string());
        }
    }
    Ok(out)
}

fn convert_to_png(source: &Path) -> Result<Vec<u8>, String> {
    // Each candidate takes the source path and writes PNG to stdout.
    let candidates: &[(&str, &[&str])] = &[
        ("ffmpeg", &["-v", "error", "-i"]),
        ("magick", &["-"]),
        ("convert", &["-"]),
    ];
    let mut last_error = String::new();
    for (binary, prefix) in candidates {
        let Some(program) = search_path(binary) else {
            continue;
        };
        let mut command = std::process::Command::new(program);
        for arg in *prefix {
            if *arg == "-" {
                command.arg(source);
                command.arg("png:-");
                break;
            }
            command.arg(arg);
        }
        if *binary == "ffmpeg" {
            command.arg(source);
            command.arg("-frames:v");
            command.arg("1");
            command.arg("-f");
            command.arg("image2pipe");
            command.arg("-vcodec");
            command.arg("png");
            command.arg("-");
        }
        command.stdin(std::process::Stdio::null());
        command.stderr(std::process::Stdio::piped());
        match command.output() {
            Ok(output) if output.status.success() && !output.stdout.is_empty() => {
                return Ok(output.stdout);
            }
            Ok(output) => {
                last_error = String::from_utf8_lossy(&output.stderr).trim().to_string();
            }
            Err(error) => last_error = error.to_string(),
        }
    }
    Err(format!(
        "no image decoder for this format was found (PNG is always supported); {last_error}"
    ))
}

fn search_path(binary: &str) -> Option<PathBuf> {
    if binary.contains('/') {
        let path = PathBuf::from(binary);
        return path.is_file().then_some(path);
    }
    let path = std::env::var_os("PATH")?;
    for directory in std::env::split_paths(&path) {
        let candidate = directory.join(binary);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

/// Aspect-fill square thumbnail with bilinear sampling.
pub fn square_thumbnail(source: &CoverImage, size: u32) -> CoverImage {
    let size = size.max(1);
    let mut out = vec![0u8; (size * size * 4) as usize];
    if source.width == 0 || source.height == 0 {
        return CoverImage {
            width: size,
            height: size,
            rgba: out,
        };
    }
    let scale = (size as f32 / source.width as f32).max(size as f32 / source.height as f32);
    let scaled_w = source.width as f32 * scale;
    let scaled_h = source.height as f32 * scale;
    let offset_x = (scaled_w - size as f32) * 0.5;
    let offset_y = (scaled_h - size as f32) * 0.5;

    for y in 0..size {
        for x in 0..size {
            let sx = ((x as f32 + 0.5 + offset_x) / scale - 0.5).max(0.0);
            let sy = ((y as f32 + 0.5 + offset_y) / scale - 0.5).max(0.0);
            let pixel = sample_bilinear(source, sx, sy);
            let index = ((y * size + x) * 4) as usize;
            out[index..index + 4].copy_from_slice(&pixel);
        }
    }
    CoverImage {
        width: size,
        height: size,
        rgba: out,
    }
}

fn sample_bilinear(image: &CoverImage, x: f32, y: f32) -> [u8; 4] {
    let max_x = image.width.saturating_sub(1) as f32;
    let max_y = image.height.saturating_sub(1) as f32;
    let x = x.clamp(0.0, max_x);
    let y = y.clamp(0.0, max_y);
    let x0 = x.floor() as u32;
    let y0 = y.floor() as u32;
    let x1 = (x0 + 1).min(image.width.saturating_sub(1));
    let y1 = (y0 + 1).min(image.height.saturating_sub(1));
    let fx = x - x0 as f32;
    let fy = y - y0 as f32;
    let at = |px: u32, py: u32, channel: usize| {
        let index = ((py * image.width + px) * 4) as usize + channel;
        image.rgba.get(index).copied().unwrap_or(0) as f32
    };
    let mut out = [0u8; 4];
    for (channel, slot) in out.iter_mut().enumerate() {
        let top = at(x0, y0, channel) * (1.0 - fx) + at(x1, y0, channel) * fx;
        let bottom = at(x0, y1, channel) * (1.0 - fx) + at(x1, y1, channel) * fx;
        *slot = (top * (1.0 - fy) + bottom * fy).round().clamp(0.0, 255.0) as u8;
    }
    out
}

fn write_png(path: &Path, image: &CoverImage) -> Result<(), String> {
    let file = std::fs::File::create(path)
        .map_err(|error| format!("Could not create {}: {error}", path.display()))?;
    let mut encoder = png::Encoder::new(file, image.width, image.height);
    encoder.set_color(png::ColorType::Rgba);
    encoder.set_depth(png::BitDepth::Eight);
    let mut writer = encoder
        .write_header()
        .map_err(|error| format!("Could not write {}: {error}", path.display()))?;
    writer
        .write_image_data(&image.rgba)
        .map_err(|error| format!("Could not write {}: {error}", path.display()))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn png_bytes(width: u32, height: u32, rgba: &[u8]) -> Vec<u8> {
        let mut out = Vec::new();
        {
            let mut encoder = png::Encoder::new(&mut out, width, height);
            encoder.set_color(png::ColorType::Rgba);
            encoder.set_depth(png::BitDepth::Eight);
            let mut writer = encoder.write_header().unwrap();
            writer.write_image_data(rgba).unwrap();
        }
        out
    }

    #[test]
    fn thumbnail_is_square_and_scaled() {
        let image = CoverImage {
            width: 8,
            height: 4,
            rgba: vec![200; 8 * 4 * 4],
        };
        let thumb = square_thumbnail(&image, 16);
        assert_eq!(thumb.width, 16);
        assert_eq!(thumb.height, 16);
        assert_eq!(thumb.rgba.len(), 16 * 16 * 4);
        assert!(thumb.rgba.iter().all(|value| *value == 200));
    }

    #[test]
    fn png_round_trip_decodes_rgba() {
        let bytes = png_bytes(2, 1, &[1, 2, 3, 4, 5, 6, 7, 8]);
        let image = decode_png(&bytes).unwrap();
        assert_eq!((image.width, image.height), (2, 1));
        assert_eq!(image.rgba, vec![1, 2, 3, 4, 5, 6, 7, 8]);
    }

    #[test]
    fn indexed_png_is_expanded_to_rgba() {
        let mut bytes = Vec::new();
        {
            let mut encoder = png::Encoder::new(&mut bytes, 2, 2);
            encoder.set_color(png::ColorType::Indexed);
            encoder.set_depth(png::BitDepth::Eight);
            encoder.set_palette(vec![255, 0, 0, 0, 255, 0]);
            let mut writer = encoder.write_header().unwrap();
            writer.write_image_data(&[0, 1, 1, 0]).unwrap();
        }
        let image = decode_png(&bytes).unwrap();
        assert_eq!((image.width, image.height), (2, 2));
        assert_eq!(&image.rgba[0..4], &[255, 0, 0, 255]);
        assert_eq!(&image.rgba[4..8], &[0, 255, 0, 255]);
    }
}
