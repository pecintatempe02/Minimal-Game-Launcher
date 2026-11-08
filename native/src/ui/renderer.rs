//! The 2D renderer: one instanced quad pipeline draws every primitive.
//!
//! Design choices that keep this small and fast:
//! * Every primitive is an *instance* of the same unit quad. A rounded
//!   rectangle, a border ring, a gradient, or a glyph is expressed with
//!   per-instance parameters instead of separate pipelines or meshes, so a whole
//!   frame is exactly one draw call.
//! * Glyphs are rasterised on the CPU (swash, through `cosmic-text`) and packed
//!   into a two-layer R8 texture array: layer 0 persists across frames for the
//!   bundled Latin subsets, layer 1 is a scratch layer for fallback fonts
//!   (CJK/emoji) that is re-packed every frame. That bounds atlas memory to
//!   2 MB while still rendering any script.
//! * Clipping is per instance (a rect evaluated in the fragment shader) instead
//!   of pipeline/scissor state changes, which keeps the frame a single pass.
//! * Colours are premultiplied in the shader so the compositor blends correctly
//!   in both the transparent and the opaque fallback mode.
//! * Geometry is rebuilt only when a redraw is requested; idle frames cost zero.

use std::collections::HashMap;

use crate::core::cover::CoverImage;
use crate::gfx::Gfx;
use crate::ui::text::{Font, TextEngine};
use crate::ui::theme::Rgba;

/// One drawable quad. The unit quad itself is generated in the vertex shader
/// from `vertex_index`, so there is no vertex buffer at all.
#[repr(C)]
#[derive(Clone, Copy, bytemuck::Pod, bytemuck::Zeroable, Debug)]
pub struct Instance {
    /// Top-left corner and size, in logical pixels.
    pub rect: [f32; 4],
    /// Fill / gradient start colour, premultiplied.
    pub color_a: [f32; 4],
    /// Gradient end colour, premultiplied.
    pub color_b: [f32; 4],
    /// Corner radii: tl, tr, br, bl.
    pub radii: [f32; 4],
    /// Border colour, premultiplied.
    pub border_color: [f32; 4],
    /// Clip rectangle in logical pixels: x, y, w, h.
    pub clip: [f32; 4],
    /// Border width in pixels; 0 disables the ring.
    pub border: f32,
    /// 0 = vertical gradient, 1 = horizontal, 2 = flat.
    pub gradient_axis: f32,
    /// Atlas layer for glyph quads, or -1 for shapes.
    pub page: f32,
    /// Atlas sub-rect of the glyph in pixels: x, y, w, h.
    pub uv: [f32; 4],
    /// Silhouette: 0 rounded rect, 1 triangle-right, 2 triangle-down,
    /// 3 circle, 4 upward-pointing triangle.
    pub shape: f32,
    /// Rotation around the rect centre in radians; icons are drawn from rotated
    /// capsules instead of shipping an icon font.
    pub rot: f32,
    /// Cover texture-array layer, or -1 for a normal primitive.
    pub cover: f32,
}

const SHADER: &str = r#"
struct Globals {
  viewport: vec2<f32>,
  _pad: vec2<f32>,
};

@group(0) @binding(0) var<uniform> globals: Globals;
@group(0) @binding(1) var atlas: texture_2d_array<f32>;
@group(0) @binding(2) var atlas_sampler: sampler;
@group(0) @binding(3) var covers: texture_2d_array<f32>;

struct VsIn {
  @builtin(vertex_index) vi: u32,
  @location(0) rect: vec4<f32>,
  @location(1) color_a: vec4<f32>,
  @location(2) color_b: vec4<f32>,
  @location(3) radii: vec4<f32>,
  @location(4) border_color: vec4<f32>,
  @location(5) clip: vec4<f32>,
  @location(6) border: f32,
  @location(7) gradient_axis: f32,
  @location(8) page: f32,
  @location(9) uv: vec4<f32>,
  @location(10) shape: f32,
  @location(11) rot: f32,
  @location(12) cover: f32,
};

struct VsOut {
  @builtin(position) pos: vec4<f32>,
  @location(0) local: vec2<f32>,
  @location(1) size: vec2<f32>,
  @location(2) color_a: vec4<f32>,
  @location(3) color_b: vec4<f32>,
  @location(4) radii: vec4<f32>,
  @location(5) border_color: vec4<f32>,
  @location(6) @interpolate(flat) clip: vec4<f32>,
  @location(7) border: f32,
  @location(8) gradient_axis: f32,
  @location(9) uv: vec4<f32>,
  @location(10) @interpolate(flat) page: f32,
  @location(11) @interpolate(flat) shape: f32,
  @location(12) pixel: vec2<f32>,
  @location(13) @interpolate(flat) rot: f32,
  @location(14) @interpolate(flat) cover: f32,
};

@vertex
fn vs(in: VsIn) -> VsOut {
  var corners = array<vec2<f32>, 6>(
    vec2<f32>(0.0, 0.0), vec2<f32>(1.0, 0.0), vec2<f32>(0.0, 1.0),
    vec2<f32>(1.0, 0.0), vec2<f32>(1.0, 1.0), vec2<f32>(0.0, 1.0),
  );
  let c = corners[in.vi];
  var px = in.rect.xy + c * in.rect.zw;
  // Rotate around the rect centre so a capsule can become a diagonal stroke.
  if (in.rot != 0.0) {
    let centre = in.rect.xy + in.rect.zw * 0.5;
    let d = px - centre;
    let cs = cos(in.rot);
    let sn = sin(in.rot);
    px = centre + vec2<f32>(d.x * cs - d.y * sn, d.x * sn + d.y * cs);
  }

  var out: VsOut;
  let ndc = vec2<f32>(px.x / globals.viewport.x * 2.0 - 1.0,
                      1.0 - px.y / globals.viewport.y * 2.0);
  out.pos = vec4<f32>(ndc, 0.0, 1.0);
  out.local = c * in.rect.zw;
  out.size = in.rect.zw;
  out.color_a = in.color_a;
  out.color_b = in.color_b;
  out.radii = in.radii;
  out.border_color = in.border_color;
  out.clip = in.clip;
  out.border = in.border;
  out.gradient_axis = in.gradient_axis;
  out.uv = in.uv;
  out.page = in.page;
  out.shape = in.shape;
  out.pixel = px;
  out.rot = in.rot;
  out.cover = in.cover;
  return out;
}

// Signed distance to a rounded rectangle; negative inside.
fn rounded_sdf(p: vec2<f32>, size: vec2<f32>, radii: vec4<f32>) -> f32 {
  var r = radii.x;
  if (p.x > size.x * 0.5) {
    if (p.y > size.y * 0.5) { r = radii.z; } else { r = radii.y; }
  } else {
    if (p.y > size.y * 0.5) { r = radii.w; } else { r = radii.x; }
  }
  let half = size * 0.5;
  let q = abs(p - half) - half + vec2<f32>(r);
  return length(max(q, vec2<f32>(0.0))) + min(max(q.x, q.y), 0.0) - r;
}

@fragment
fn fs(in: VsOut) -> @location(0) vec4<f32> {
  // Hard clip to the current scroll/panel rectangle.
  if (in.pixel.x < in.clip.x || in.pixel.y < in.clip.y ||
      in.pixel.x > in.clip.x + in.clip.z || in.pixel.y > in.clip.y + in.clip.w) {
    discard;
  }

  // --- cover image ---
  if (in.cover >= 0.0) {
    let uv = in.local / max(in.size, vec2<f32>(1.0));
    let sample = textureSampleLevel(covers, atlas_sampler, uv, i32(in.cover), 0.0);
    let d = rounded_sdf(in.local, in.size, in.radii);
    let alpha = clamp(0.5 - d, 0.0, 1.0) * in.color_a.a;
    return vec4<f32>(sample.rgb * sample.a, sample.a) * alpha;
  }

  // --- text ---
  if (in.page >= 0.0) {
    let uv = (in.uv.xy + (in.local / max(in.size, vec2<f32>(1.0))) * in.uv.zw) / 1024.0;
    let mask = textureSample(atlas, atlas_sampler, uv, i32(in.page)).r;
    return in.color_a * mask;
  }

  // --- shapes ---
  var d = 0.0;
  if (in.shape == 1.0) {
    // Right-pointing triangle: distance to the three edges.
    let p = in.local;
    let s = in.size;
    // Triangle (0,0) (s.x, s.y/2) (0, s.y); negative inside.
    let e0 = (p.x * s.y * 0.5 + abs(p.y - s.y * 0.5) * s.x - s.x * s.y * 0.5)
             / length(vec2<f32>(s.y * 0.5, s.x));
    d = e0;
  } else if (in.shape == 2.0) {
    // Down-pointing triangle.
    let p = in.local;
    let s = in.size;
    let e0 = (p.y * s.x * 0.5 + abs(p.x - s.x * 0.5) * s.y - s.x * s.y * 0.5)
             / length(vec2<f32>(s.x * 0.5, s.y));
    d = e0;
  } else if (in.shape == 4.0) {
    // Up-pointing triangle.
    let p = vec2<f32>(in.local.x, in.size.y - in.local.y);
    let s = in.size;
    let e0 = (p.y * s.x * 0.5 + abs(p.x - s.x * 0.5) * s.y - s.x * s.y * 0.5)
             / length(vec2<f32>(s.x * 0.5, s.y));
    d = e0;
  } else if (in.shape == 3.0) {
    // Circle: exact signed distance to the ellipse-free circumscribed circle.
    let r = min(in.size.x, in.size.y) * 0.5;
    d = length(in.local - in.size * 0.5) - r;
  } else {
    d = rounded_sdf(in.local, in.size, in.radii);
  }
  // One-pixel analytic edge ramp, no derivative instructions required.
  let alpha = clamp(0.5 - d, 0.0, 1.0);

  var t = 0.0;
  if (in.gradient_axis == 0.0) {
    t = clamp(in.local.y / max(in.size.y, 1.0), 0.0, 1.0);
  } else if (in.gradient_axis == 1.0) {
    t = clamp(in.local.x / max(in.size.x, 1.0), 0.0, 1.0);
  } else if (in.gradient_axis == 3.0) {
    // CSS `linear-gradient(145deg, …)`: direction (sin, -cos), gradient line
    // length |w·sin| + |h·cos|.
    let dir = vec2<f32>(0.573576, 0.819152);
    let len = max(in.size.x * 0.573576 + in.size.y * 0.819152, 1.0);
    let p = in.local - in.size * 0.5;
    t = clamp(dot(p, dir) / len + 0.5, 0.0, 1.0);
  }
  let color = mix(in.color_a, in.color_b, t);

  if (in.border > 0.0) {
    // `inside` is the coverage of the inset shape, so `alpha - inside` is the
    // band between the outline and the inset edge: exactly the border ring.
    let inside = clamp(0.5 - (d + in.border), 0.0, 1.0);
    let ring = alpha - inside;
    return color * inside + in.border_color * ring;
  }
  return color * alpha;
}
"#;

/// Non-rectangular silhouettes the shader can rasterise analytically.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u32)]
pub enum Shape {
    /// Axis-aligned rounded rectangle (the default for `rect`).
    Rect = 0,
    /// Right-pointing triangle: the "Play" control.
    TriangleRight = 1,
    /// Down-pointing triangle: a menu affordance.
    TriangleDown = 2,
    /// Circle: radio dots and knob tracks.
    Circle = 3,
    /// Up-pointing triangle.
    TriangleUp = 4,
}

/// Side of every atlas layer, in pixels. Two layers cost 2 MB of VRAM total.
const PAGE_SIZE: u32 = 1024;
/// Atlas layers: 0 is persistent, 1 is the per-frame scratch layer.
const LAYERS: u32 = 2;
const SCRATCH_LAYER: u32 = 1;
/// Cover thumbnails live in a separate colour texture array. 16 × 384² RGBA is
/// about 9 MB and is reused round-robin when more games are visible than layers.
const COVER_SIZE: u32 = 384;
const COVER_LAYERS: u32 = 16;
/// Cap on glyphs cached in the persistent layer. Beyond this, glyphs are served
/// from the scratch layer rather than growing memory.
const PERSISTENT_CACHE_CAPACITY: usize = 6144;
/// Hard cap on instances per frame; ~3.5 MB at 115 bytes each.
const MAX_INSTANCES: usize = 32768;

/// Where a glyph's mask lives, plus its placement relative to the pen position.
#[derive(Clone, Copy)]
struct GlyphSlot {
    layer: u32,
    x: u32,
    y: u32,
    w: u32,
    h: u32,
    left: i32,
    top: i32,
}

/// Shelf packer for one atlas layer.
struct Packer {
    shelf_y: u32,
    shelf_h: u32,
    cursor_x: u32,
}

impl Packer {
    fn new() -> Self {
        Self {
            shelf_y: 0,
            shelf_h: 0,
            cursor_x: 0,
        }
    }

    fn reset(&mut self) {
        *self = Self::new();
    }

    /// Reserve `w`x`h` pixels, returning the origin, or `None` when full.
    fn alloc(&mut self, w: u32, h: u32) -> Option<(u32, u32)> {
        if w == 0 || h == 0 || w > PAGE_SIZE || h > PAGE_SIZE {
            return None;
        }
        if self.cursor_x + w > PAGE_SIZE {
            self.shelf_y += self.shelf_h + 1;
            self.shelf_h = 0;
            self.cursor_x = 0;
        }
        if self.shelf_y + h > PAGE_SIZE {
            return None;
        }
        let pos = (self.cursor_x, self.shelf_y);
        self.cursor_x += w + 1;
        self.shelf_h = self.shelf_h.max(h);
        Some(pos)
    }
}

pub struct Renderer {
    pipeline: wgpu::RenderPipeline,
    globals: wgpu::Buffer,
    instance_buffer: wgpu::Buffer,
    bind_group: wgpu::BindGroup,
    atlas: wgpu::Texture,
    /// Colour texture array used by cover thumbnails.
    covers: wgpu::Texture,
    /// Key → layer mapping, so a thumbnail is uploaded once per renderer.
    cover_slots: HashMap<String, u8>,
    cover_next: u8,
    cover_uploads: Vec<(u8, u32, u32, Vec<u8>)>,
    /// CPU-side instance list, reused every frame.
    instances: Vec<Instance>,
    /// Persistent glyph cache (atlas layer 0).
    persistent: HashMap<cosmic_text::CacheKey, GlyphSlot>,
    persistent_packer: Packer,
    /// Scratch glyph cache for the current frame (atlas layer 1).
    scratch: HashMap<cosmic_text::CacheKey, GlyphSlot>,
    scratch_packer: Packer,
    /// Pending texture uploads for the current frame.
    uploads: Vec<(u32, u32, u32, u32, u32, Vec<u8>)>,
    /// Active clip rectangle, restored by [`Renderer::pop_clip`].
    clip: [f32; 4],
    clip_stack: Vec<[f32; 4]>,
    /// Device pixels per logical pixel. Everything the UI hands over is in
    /// logical pixels; the instances are built at device resolution so text is
    /// rasterised at the real pixel size instead of being upscaled.
    scale: f32,
    /// True when the window composites alpha; only affects the clear colour.
    pub transparent: bool,
}

impl Renderer {
    pub fn new(gfx: &Gfx) -> Self {
        Self::with_device(&gfx.device, gfx.config.format)
    }

    /// Build the pipeline for an arbitrary device and colour format.
    ///
    /// `format` must match the render target, so the offscreen harness passes its
    /// own instead of the swapchain's.
    pub fn with_device(device: &wgpu::Device, format: wgpu::TextureFormat) -> Self {
        let shader = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("ui"),
            source: wgpu::ShaderSource::Wgsl(SHADER.into()),
        });
        let bind_layout = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
            label: Some("ui"),
            entries: &[
                wgpu::BindGroupLayoutEntry {
                    binding: 0,
                    visibility: wgpu::ShaderStages::VERTEX,
                    ty: wgpu::BindingType::Buffer {
                        ty: wgpu::BufferBindingType::Uniform,
                        has_dynamic_offset: false,
                        min_binding_size: None,
                    },
                    count: None,
                },
                wgpu::BindGroupLayoutEntry {
                    binding: 1,
                    visibility: wgpu::ShaderStages::FRAGMENT,
                    ty: wgpu::BindingType::Texture {
                        sample_type: wgpu::TextureSampleType::Float { filterable: true },
                        view_dimension: wgpu::TextureViewDimension::D2Array,
                        multisampled: false,
                    },
                    count: None,
                },
                wgpu::BindGroupLayoutEntry {
                    binding: 2,
                    visibility: wgpu::ShaderStages::FRAGMENT,
                    ty: wgpu::BindingType::Sampler(wgpu::SamplerBindingType::Filtering),
                    count: None,
                },
                wgpu::BindGroupLayoutEntry {
                    binding: 3,
                    visibility: wgpu::ShaderStages::FRAGMENT,
                    ty: wgpu::BindingType::Texture {
                        sample_type: wgpu::TextureSampleType::Float { filterable: true },
                        view_dimension: wgpu::TextureViewDimension::D2Array,
                        multisampled: false,
                    },
                    count: None,
                },
            ],
        });
        let sampler = device.create_sampler(&wgpu::SamplerDescriptor {
            label: Some("glyph"),
            mag_filter: wgpu::FilterMode::Linear,
            min_filter: wgpu::FilterMode::Linear,
            address_mode_u: wgpu::AddressMode::ClampToEdge,
            address_mode_v: wgpu::AddressMode::ClampToEdge,
            ..Default::default()
        });
        let globals = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("globals"),
            size: 16,
            usage: wgpu::BufferUsages::UNIFORM | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });
        let atlas = device.create_texture(&wgpu::TextureDescriptor {
            label: Some("glyph-atlas"),
            size: wgpu::Extent3d {
                width: PAGE_SIZE,
                height: PAGE_SIZE,
                depth_or_array_layers: LAYERS,
            },
            mip_level_count: 1,
            sample_count: 1,
            dimension: wgpu::TextureDimension::D2,
            format: wgpu::TextureFormat::R8Unorm,
            usage: wgpu::TextureUsages::TEXTURE_BINDING | wgpu::TextureUsages::COPY_DST,
            view_formats: &[],
        });
        let atlas_view = atlas.create_view(&wgpu::TextureViewDescriptor {
            dimension: Some(wgpu::TextureViewDimension::D2Array),
            ..Default::default()
        });
        let covers = device.create_texture(&wgpu::TextureDescriptor {
            label: Some("cover-atlas"),
            size: wgpu::Extent3d {
                width: COVER_SIZE,
                height: COVER_SIZE,
                depth_or_array_layers: COVER_LAYERS,
            },
            mip_level_count: 1,
            sample_count: 1,
            dimension: wgpu::TextureDimension::D2,
            format: wgpu::TextureFormat::Rgba8Unorm,
            usage: wgpu::TextureUsages::TEXTURE_BINDING | wgpu::TextureUsages::COPY_DST,
            view_formats: &[],
        });
        let cover_view = covers.create_view(&wgpu::TextureViewDescriptor {
            dimension: Some(wgpu::TextureViewDimension::D2Array),
            ..Default::default()
        });
        let bind_group = device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: Some("ui"),
            layout: &bind_layout,
            entries: &[
                wgpu::BindGroupEntry {
                    binding: 0,
                    resource: globals.as_entire_binding(),
                },
                wgpu::BindGroupEntry {
                    binding: 1,
                    resource: wgpu::BindingResource::TextureView(&atlas_view),
                },
                wgpu::BindGroupEntry {
                    binding: 2,
                    resource: wgpu::BindingResource::Sampler(&sampler),
                },
                wgpu::BindGroupEntry {
                    binding: 3,
                    resource: wgpu::BindingResource::TextureView(&cover_view),
                },
            ],
        });
        let pipeline_layout = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
            label: Some("ui"),
            bind_group_layouts: &[Some(&bind_layout)],
            immediate_size: 0,
        });
        let pipeline = device.create_render_pipeline(&wgpu::RenderPipelineDescriptor {
            label: Some("ui"),
            layout: Some(&pipeline_layout),
            vertex: wgpu::VertexState {
                module: &shader,
                entry_point: Some("vs"),
                compilation_options: Default::default(),
                buffers: &[Some(wgpu::VertexBufferLayout {
                    array_stride: std::mem::size_of::<Instance>() as u64,
                    step_mode: wgpu::VertexStepMode::Instance,
                    attributes: &wgpu::vertex_attr_array![
                        0 => Float32x4, // rect
                        1 => Float32x4, // color_a
                        2 => Float32x4, // color_b
                        3 => Float32x4, // radii
                        4 => Float32x4, // border_color
                        5 => Float32x4, // clip
                        6 => Float32,   // border
                        7 => Float32,   // gradient_axis
                        8 => Float32,   // page
                        9 => Float32x4, // uv
                        10 => Float32,  // shape
                        11 => Float32,  // rot
                        12 => Float32,  // cover layer
                    ],
                })],
            },
            fragment: Some(wgpu::FragmentState {
                module: &shader,
                entry_point: Some("fs"),
                compilation_options: Default::default(),
                targets: &[Some(wgpu::ColorTargetState {
                    format,
                    // Premultiplied source over premultiplied destination; also
                    // correct for the opaque fallback because the window
                    // background is painted fully opaque first.
                    blend: Some(wgpu::BlendState::PREMULTIPLIED_ALPHA_BLENDING),
                    write_mask: wgpu::ColorWrites::ALL,
                })],
            }),
            primitive: wgpu::PrimitiveState::default(),
            depth_stencil: None,
            multisample: wgpu::MultisampleState::default(),
            multiview_mask: None,
            cache: None,
        });

        let instance_buffer = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("instances"),
            size: (MAX_INSTANCES * std::mem::size_of::<Instance>()) as u64,
            usage: wgpu::BufferUsages::VERTEX | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });

        Self {
            pipeline,
            globals,
            instance_buffer,
            bind_group,
            atlas,
            covers,
            cover_slots: HashMap::new(),
            cover_next: 0,
            cover_uploads: Vec::new(),
            instances: Vec::with_capacity(4096),
            persistent: HashMap::new(),
            persistent_packer: Packer::new(),
            scratch: HashMap::new(),
            scratch_packer: Packer::new(),
            uploads: Vec::new(),
            clip: [0.0; 4],
            clip_stack: Vec::new(),
            scale: 1.0,
            transparent: true,
        }
    }

    /// Begin a frame: reset the instance list, the scratch layer, and the clip.
    ///
    /// The scratch glyph cache is dropped every frame; it only ever holds the
    /// handful of fallback glyphs currently on screen.
    pub fn begin(&mut self, width: f32, height: f32) {
        self.instances.clear();
        self.uploads.clear();
        self.cover_uploads.clear();
        if !self.scratch.is_empty() {
            self.scratch.clear();
            self.scratch_packer.reset();
        }
        self.clip = [0.0, 0.0, width * self.scale, height * self.scale];
        self.clip_stack.clear();
    }

    /// Set the device scale. The UI keeps working in logical pixels; instances are
    /// emitted at device resolution so glyph masks match the framebuffer.
    pub fn set_scale(&mut self, scale: f32) {
        self.scale = scale.clamp(0.5, 4.0);
    }

    /// Convert a logical rect into the device-pixel space the shader works in.
    fn dev(&self, rect: [f32; 4]) -> [f32; 4] {
        let s = self.scale;
        [rect[0] * s, rect[1] * s, rect[2] * s, rect[3] * s]
    }

    pub fn clip(&self) -> [f32; 4] {
        self.clip
    }

    /// Intersect the current clip with `rect` (typically a scroll viewport).
    pub fn push_clip(&mut self, rect: [f32; 4]) {
        let rect = self.dev(rect);
        self.clip_stack.push(self.clip);
        let x0 = self.clip[0].max(rect[0]);
        let y0 = self.clip[1].max(rect[1]);
        let x1 = (self.clip[0] + self.clip[2]).min(rect[0] + rect[2]);
        let y1 = (self.clip[1] + self.clip[3]).min(rect[1] + rect[3]);
        self.clip = [x0, y0, (x1 - x0).max(0.0), (y1 - y0).max(0.0)];
    }

    pub fn pop_clip(&mut self) {
        if let Some(previous) = self.clip_stack.pop() {
            self.clip = previous;
        }
    }

    /// Whether a rectangle is completely outside the current clip.
    pub fn culled(&self, rect: [f32; 4]) -> bool {
        rect[0] + rect[2] < self.clip[0]
            || rect[1] + rect[3] < self.clip[1]
            || rect[0] > self.clip[0] + self.clip[2]
            || rect[1] > self.clip[1] + self.clip[3]
    }

    // ---------------------------------------------------------------- shapes

    /// Flat rectangle, optionally rounded.
    pub fn rect(&mut self, rect: [f32; 4], color: Rgba, radius: f32) {
        self.rect_multi(rect, color, color, radius, 0.0, Rgba::TRANSPARENT, 2.0);
    }

    /// Rectangle filled with a two-stop gradient.
    pub fn gradient(&mut self, rect: [f32; 4], a: Rgba, b: Rgba, radius: f32, vertical: bool) {
        self.rect_multi(
            rect,
            a,
            b,
            radius,
            0.0,
            Rgba::TRANSPARENT,
            if vertical { 0.0 } else { 1.0 },
        );
    }

    /// `linear-gradient(145deg, a, b)` — used for the generated cover art.
    pub fn gradient_diag(&mut self, rect: [f32; 4], a: Rgba, b: Rgba, radius: f32) {
        self.rect_multi(rect, a, b, radius, 0.0, Rgba::TRANSPARENT, 3.0);
    }

    /// Draw a cover thumbnail, rounded like a card.
    ///
    /// The first draw of a key uploads its pixels to a layer of the cover
    /// texture array; later frames only add one instance. When more covers are
    /// shown than there are layers, slots are reused round-robin.
    pub fn cover(&mut self, key: &str, image: &CoverImage, rect: [f32; 4], radius: f32) {
        if rect[2] <= 0.0 || rect[3] <= 0.0 || self.instances.len() >= MAX_INSTANCES {
            return;
        }
        if image.width == 0
            || image.height == 0
            || image.width > COVER_SIZE
            || image.height > COVER_SIZE
        {
            return;
        }
        let slot = match self.cover_slots.get(key) {
            Some(slot) => *slot,
            None => {
                let slot = self.cover_next % COVER_LAYERS as u8;
                self.cover_next = (self.cover_next + 1) % COVER_LAYERS as u8;
                // Forget whichever key used this layer before.
                if let Some(old) = self
                    .cover_slots
                    .iter()
                    .find(|(_, used)| **used == slot)
                    .map(|(old, _)| old.clone())
                {
                    self.cover_slots.remove(&old);
                }
                self.cover_slots.insert(key.to_string(), slot);
                self.cover_uploads
                    .push((slot, image.width, image.height, image.rgba.clone()));
                slot
            }
        };
        let rect = self.dev(rect);
        let r = (radius * self.scale).clamp(0.0, rect[2].min(rect[3]) * 0.5);
        self.instances.push(Instance {
            rect,
            color_a: Rgba::rgb(255, 255, 255).to_premul(),
            color_b: Rgba::rgb(255, 255, 255).to_premul(),
            radii: [r; 4],
            border_color: [0.0; 4],
            clip: self.clip,
            border: 0.0,
            gradient_axis: 2.0,
            page: -1.0,
            uv: [0.0; 4],
            shape: 0.0,
            rot: 0.0,
            cover: slot as f32,
        });
    }

    /// Rectangle with an independent border ring.
    ///
    /// Two instances on purpose: CSS paints the border over the element's own
    /// background, so the ring has to be blended against the fill and not
    /// against whatever was on the page beneath it.
    pub fn rect_border(
        &mut self,
        rect: [f32; 4],
        fill: Rgba,
        radius: f32,
        border: f32,
        border_color: Rgba,
    ) {
        if fill.a > 0.0 {
            self.rect(rect, fill, radius);
        }
        self.rect_multi(
            rect,
            Rgba::TRANSPARENT,
            Rgba::TRANSPARENT,
            radius,
            border,
            border_color,
            2.0,
        );
    }

    /// Horizontal hairline, snapped to the pixel grid to stay crisp.
    pub fn hline(&mut self, x: f32, y: f32, width: f32, color: Rgba) {
        let y = y.round();
        self.rect([x, y, width, 1.0], color, 0.0);
    }

    /// Draw any of the shader's analytic silhouettes. The bundled font subsets
    /// contain no symbol glyphs, so every icon is composed from these.
    pub fn shape(&mut self, shape: Shape, rect: [f32; 4], color: Rgba, rot: f32) {
        if rect[2] <= 0.0 || rect[3] <= 0.0 || self.instances.len() >= MAX_INSTANCES {
            return;
        }
        let rect = self.dev(rect);
        self.instances.push(Instance {
            rect,
            color_a: color.to_premul(),
            color_b: color.to_premul(),
            radii: [0.0; 4],
            border_color: [0.0; 4],
            clip: self.clip,
            border: 0.0,
            gradient_axis: 2.0,
            page: -1.0,
            uv: [0.0; 4],
            shape: shape as u32 as f32,
            rot,
            cover: -1.0,
        });
    }

    /// A square, fully rounded capsule: the primitive every line-based icon is
    /// built from (bars, crosses, chevrons).
    pub fn capsule(&mut self, x: f32, y: f32, w: f32, h: f32, color: Rgba, rot: f32) {
        let rect = self.dev([x, y, w, h]);
        if self.instances.len() >= MAX_INSTANCES {
            return;
        }
        self.instances.push(Instance {
            rect,
            color_a: color.to_premul(),
            color_b: color.to_premul(),
            radii: [h.min(w) * 0.5; 4],
            border_color: [0.0; 4],
            clip: self.clip,
            border: 0.0,
            gradient_axis: 2.0,
            page: -1.0,
            uv: [0.0; 4],
            shape: 0.0,
            rot,
            cover: -1.0,
        });
    }

    /// A centred symbol inside `box_rect`, sized to `size`.
    pub fn icon(&mut self, shape: Shape, box_rect: [f32; 4], size: f32, color: Rgba, rot: f32) {
        let w = match shape {
            Shape::Circle => size,
            _ => size,
        };
        let h = size;
        let x = box_rect[0] + (box_rect[2] - w) * 0.5;
        let y = box_rect[1] + (box_rect[3] - h) * 0.5;
        self.shape(shape, [x, y, w, h], color, rot);
    }

    #[allow(clippy::too_many_arguments)]
    fn rect_multi(
        &mut self,
        rect: [f32; 4],
        a: Rgba,
        b: Rgba,
        radius: f32,
        border: f32,
        border_color: Rgba,
        axis: f32,
    ) {
        if rect[2] <= 0.0 || rect[3] <= 0.0 || self.instances.len() >= MAX_INSTANCES {
            return;
        }
        let border = border * self.scale;
        let rect = self.dev(rect);
        let r = radius * self.scale;
        let r = r.clamp(0.0, rect[2].min(rect[3]) * 0.5);
        self.instances.push(Instance {
            rect,
            color_a: a.to_premul(),
            color_b: if axis == 2.0 {
                a.to_premul()
            } else {
                b.to_premul()
            },
            radii: [r; 4],
            border_color: border_color.to_premul(),
            clip: self.clip,
            border,
            gradient_axis: axis,
            page: -1.0,
            uv: [0.0; 4],
            shape: 0.0,
            rot: 0.0,
            cover: -1.0,
        });
    }

    // ------------------------------------------------------------------ text

    /// Draw a single line of text, ellipsized to `rect[2]`.
    ///
    /// Returns the shaped height so callers can stack content below it.
    pub fn text(
        &mut self,
        texts: &mut TextEngine,
        text: &str,
        rect: [f32; 4],
        font: Font,
        size: f32,
        color: Rgba,
    ) -> f32 {
        let fitted = texts.ellipsize(text, font, size, rect[2]);
        self.text_raw(texts, &fitted, rect, font, size, color, false, 0.0)
    }

    /// Draw wrapped text inside `rect`; the caller owns any height budget.
    pub fn text_wrapped(
        &mut self,
        texts: &mut TextEngine,
        text: &str,
        rect: [f32; 4],
        font: Font,
        size: f32,
        color: Rgba,
    ) -> f32 {
        self.text_raw(texts, text, rect, font, size, color, true, 0.0)
    }

    /// Wrapped text with an explicit CSS line height (`.code`, `textarea`).
    #[allow(clippy::too_many_arguments)]
    pub fn text_wrapped_lh(
        &mut self,
        texts: &mut TextEngine,
        text: &str,
        rect: [f32; 4],
        font: Font,
        size: f32,
        color: Rgba,
        line_height: f32,
    ) -> f32 {
        self.text_raw(texts, text, rect, font, size, color, true, line_height)
    }

    #[allow(clippy::too_many_arguments)]
    fn text_raw(
        &mut self,
        texts: &mut TextEngine,
        text: &str,
        rect: [f32; 4],
        font: Font,
        size: f32,
        color: Rgba,
        wrap: bool,
        line_height: f32,
    ) -> f32 {
        if text.is_empty() || rect[2] <= 0.0 {
            return 0.0;
        }
        // Copy the glyph list out of the shape so the atlas mutably borrows the
        // engine; the list is small and reused per frame.
        let (glyphs, height) = {
            let shape = texts.shape_lh(text, font, size, rect[2], wrap, line_height);
            (shape.glyphs.clone(), shape.height)
        };
        // A single line is centred in its box, which is what every widget in the
        // prototype relies on (`align-items: center`). Wrapped text flows from
        // the top, as a paragraph should.
        let dy = if wrap || rect[3] <= height {
            0.0
        } else {
            (rect[3] - height) * 0.5
        };
        for glyph in glyphs {
            if self.instances.len() >= MAX_INSTANCES {
                break;
            }
            let Some(slot) = self.slot(texts, glyph.key) else {
                continue;
            };
            let x = (rect[0] + glyph.x as f32) * self.scale + slot.left as f32;
            let y = (rect[1] + dy + glyph.y as f32) * self.scale - slot.top as f32;
            self.instances.push(Instance {
                rect: [x, y, slot.w as f32, slot.h as f32],
                color_a: color.to_premul(),
                color_b: color.to_premul(),
                radii: [0.0; 4],
                border_color: [0.0; 4],
                clip: self.clip,
                border: 0.0,
                gradient_axis: 2.0,
                page: slot.layer as f32,
                shape: 0.0,
                rot: 0.0,
                cover: -1.0,
                uv: [slot.x as f32, slot.y as f32, slot.w as f32, slot.h as f32],
            });
        }
        height
    }

    /// Position of a glyph in the atlas, rasterising and packing it on a miss.
    fn slot(&mut self, texts: &mut TextEngine, key: cosmic_text::CacheKey) -> Option<GlyphSlot> {
        if let Some(slot) = self.persistent.get(&key) {
            return Some(*slot);
        }
        if let Some(slot) = self.scratch.get(&key) {
            return Some(*slot);
        }
        let glyph = texts.rasterize(key)?;
        let (w, h) = (glyph.width, glyph.height);

        // Bundled faces are packed into the persistent layer; anything that
        // needed a system fallback goes to the scratch layer so a stray script
        // can never displace the Latin glyphs used every frame.
        let mut layer = 0u32;
        let mut origin = if glyph.bundled {
            self.persistent_packer.alloc(w, h)
        } else {
            None
        };
        if origin.is_none() {
            layer = SCRATCH_LAYER;
            origin = self.scratch_packer.alloc(w, h);
        }
        let (x, y) = origin?;

        let data = pack_coverage(&glyph);
        self.uploads.push((layer, x, y, w, h, data));

        let slot = GlyphSlot {
            layer,
            x,
            y,
            w,
            h,
            left: glyph.left,
            top: glyph.top,
        };
        if layer == 0 && self.persistent.len() < PERSISTENT_CACHE_CAPACITY {
            self.persistent.insert(key, slot);
        } else {
            self.scratch.insert(key, slot);
        }
        Some(slot)
    }

    // ------------------------------------------------------------------ draw

    /// Upload instances and glyph masks, then draw everything in one pass.
    ///
    /// `background` is used for the clear: the theme background at the window
    /// opacity when transparent, or fully opaque for the solid fallback.
    pub fn paint(&mut self, gfx: &mut Gfx, background: Option<Rgba>) {
        let Some(frame) = gfx.acquire() else {
            return;
        };
        let width = gfx.config.width as f32;
        let height = gfx.config.height as f32;
        let view = frame
            .texture
            .create_view(&wgpu::TextureViewDescriptor::default());
        self.draw(&gfx.device, &gfx.queue, &view, width, height, background);
        gfx.present(frame);
    }

    /// Encode one frame into `view`, sized `width`×`height` device pixels.
    ///
    /// Split from [`Renderer::paint`] so the same draw path can target an
    /// offscreen texture — used by the screenshot harness, which must render a
    /// screen without a compositor in the loop.
    pub fn draw(
        &mut self,
        device: &wgpu::Device,
        queue: &wgpu::Queue,
        view: &wgpu::TextureView,
        width: f32,
        height: f32,
        background: Option<Rgba>,
    ) {
        queue.write_buffer(
            &self.globals,
            0,
            bytemuck::cast_slice(&[width, height, 0.0, 0.0]),
        );

        // Take the upload list out first: draining `self.uploads` while also
        // borrowing `self.atlas` would need a split borrow.
        let uploads = std::mem::take(&mut self.uploads);
        for (layer, x, y, w, h, data) in uploads {
            queue.write_texture(
                wgpu::TexelCopyTextureInfo {
                    texture: &self.atlas,
                    mip_level: 0,
                    origin: wgpu::Origin3d { x, y, z: layer },
                    aspect: wgpu::TextureAspect::All,
                },
                &data,
                wgpu::TexelCopyBufferLayout {
                    offset: 0,
                    bytes_per_row: Some(w),
                    rows_per_image: Some(h),
                },
                wgpu::Extent3d {
                    width: w,
                    height: h,
                    depth_or_array_layers: 1,
                },
            );
        }

        let cover_uploads = std::mem::take(&mut self.cover_uploads);
        for (layer, width, height, data) in cover_uploads {
            queue.write_texture(
                wgpu::TexelCopyTextureInfo {
                    texture: &self.covers,
                    mip_level: 0,
                    origin: wgpu::Origin3d {
                        x: 0,
                        y: 0,
                        z: layer as u32,
                    },
                    aspect: wgpu::TextureAspect::All,
                },
                &data,
                wgpu::TexelCopyBufferLayout {
                    offset: 0,
                    bytes_per_row: Some(width * 4),
                    rows_per_image: Some(height),
                },
                wgpu::Extent3d {
                    width,
                    height,
                    depth_or_array_layers: 1,
                },
            );
        }

        if !self.instances.is_empty() {
            queue.write_buffer(
                &self.instance_buffer,
                0,
                bytemuck::cast_slice(&self.instances),
            );
        }

        let clear = match background {
            Some(color) => wgpu::Color {
                r: (color.r * color.a) as f64,
                g: (color.g * color.a) as f64,
                b: (color.b * color.a) as f64,
                a: color.a as f64,
            },
            None => wgpu::Color::TRANSPARENT,
        };
        let mut encoder =
            device.create_command_encoder(&wgpu::CommandEncoderDescriptor { label: Some("ui") });
        {
            let mut pass = encoder.begin_render_pass(&wgpu::RenderPassDescriptor {
                label: Some("ui"),
                color_attachments: &[Some(wgpu::RenderPassColorAttachment {
                    view,
                    resolve_target: None,
                    depth_slice: None,
                    ops: wgpu::Operations {
                        load: wgpu::LoadOp::Clear(clear),
                        store: wgpu::StoreOp::Store,
                    },
                })],
                multiview_mask: None,
                depth_stencil_attachment: None,
                timestamp_writes: None,
                occlusion_query_set: None,
            });
            if !self.instances.is_empty() {
                pass.set_pipeline(&self.pipeline);
                pass.set_bind_group(0, &self.bind_group, &[]);
                pass.set_vertex_buffer(0, self.instance_buffer.slice(..));
                pass.draw(0..6, 0..self.instances.len() as u32);
            }
        }
        queue.submit([encoder.finish()]);
    }
}

/// Flatten a rasterised glyph into an R8 coverage buffer for upload.
fn pack_coverage(glyph: &crate::ui::text::RasterGlyph) -> Vec<u8> {
    if !glyph.color {
        return glyph.data.clone();
    }
    // Colour glyphs (emoji) are approximated by their own alpha so they stay
    // visible without a second RGBA atlas.
    let mut out = Vec::with_capacity(glyph.data.len() / 4);
    for px in glyph.data.chunks(4) {
        out.push(px[3]);
    }
    out
}
