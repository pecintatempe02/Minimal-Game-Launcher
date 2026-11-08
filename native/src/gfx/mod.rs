//! GPU plumbing shared by the application and the rendering spike.
//!
//! Responsibilities kept here, deliberately small:
//! * create a `wgpu::Instance` without any async runtime (a tiny `pollster` block
//!   is used once per adapter/device request during startup);
//! * create a transparent-capable surface for a `winit` window;
//! * own the device/queue/swapchain lifecycle and the resize path.
//!
//! The application is event driven: [`Gfx::acquire`] is only called when a
//! redraw has been requested (input, resize, theme change, process state change).
//! Nothing in this module schedules frames.

use std::sync::Arc;

use winit::window::Window;

/// GPU state without a window: a device and queue, for offscreen rendering.
///
/// The screenshot harness uses this so it can draw a screen at an exact size
/// without a compositor resizing, tiling or upscaling it.
pub struct Headless {
    pub device: wgpu::Device,
    pub queue: wgpu::Queue,
    pub format: wgpu::TextureFormat,
}

impl Headless {
    /// Create a device on the same backends [`Gfx`] prefers.
    pub fn new() -> Result<Self, String> {
        let want_gl = std::env::var("MGL_BACKEND")
            .map(|v| v.eq_ignore_ascii_case("gl"))
            .unwrap_or(false);
        let mut desc = wgpu::InstanceDescriptor::new_without_display_handle();
        desc.backends = if want_gl {
            wgpu::Backends::GL
        } else {
            wgpu::Backends::VULKAN
        };
        let instance = wgpu::Instance::new(desc);
        let adapter = pollster::block_on(instance.request_adapter(&wgpu::RequestAdapterOptions {
            power_preference: wgpu::PowerPreference::LowPower,
            force_fallback_adapter: false,
            compatible_surface: None,
            apply_limit_buckets: false,
        }))
        .map_err(|e| format!("no usable GPU adapter: {e}"))?;
        let (device, queue) = pollster::block_on(adapter.request_device(&wgpu::DeviceDescriptor {
            label: Some("mgl-headless"),
            required_features: wgpu::Features::empty(),
            required_limits: wgpu::Limits::downlevel_defaults(),
            experimental_features: wgpu::ExperimentalFeatures::disabled(),
            memory_hints: wgpu::MemoryHints::MemoryUsage,
            trace: wgpu::Trace::Off,
        }))
        .map_err(|e| format!("could not create a GPU device: {e}"))?;
        Ok(Self {
            device,
            queue,
            format: wgpu::TextureFormat::Bgra8Unorm,
        })
    }
}

/// Everything the GPU side needs for one window.
pub struct Gfx {
    pub instance: wgpu::Instance,
    pub surface: wgpu::Surface<'static>,
    pub device: wgpu::Device,
    pub queue: wgpu::Queue,
    pub config: wgpu::SurfaceConfiguration,
    /// True when the surface can actually composite alpha (Wayland `PreMultiplied`).
    pub transparent: bool,
    /// Adapter name, only reported in diagnostic output.
    pub adapter_name: String,
}

/// Which alpha behaviour the window should use.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Alpha {
    /// Window alpha is provided to the compositor (the compositor does the blur).
    Transparent,
    /// Fully opaque window: the solid fallback.
    Opaque,
}

impl Gfx {
    /// Build the surface and the device for `window`.
    ///
    /// `display` is the event loop's owned display handle. wgpu only needs it on
    /// the GLES backend, but passing it is free and keeps Wayland presentation
    /// working when Vulkan is unavailable.
    pub fn new(
        display: Box<dyn wgpu::wgt::WgpuHasDisplayHandle>,
        window: Arc<Window>,
        alpha: Alpha,
    ) -> Result<Self, String> {
        // Vulkan only by default. Creating a GL instance makes Mesa load
        // `libgallium` + `libLLVM` (~50 MB of RSS) even when Vulkan is the
        // backend in use, which alone would break the memory budget.
        // `MGL_BACKEND=gl` opts into the GL path for machines without Vulkan.
        let want_gl = std::env::var("MGL_BACKEND")
            .map(|v| v.eq_ignore_ascii_case("gl"))
            .unwrap_or(false);

        let mut desc = wgpu::InstanceDescriptor::new_with_display_handle(display);
        desc.backends = if want_gl {
            wgpu::Backends::GL
        } else {
            wgpu::Backends::VULKAN
        };
        let instance = wgpu::Instance::new(desc);

        let surface = instance
            .create_surface(window.clone())
            .map_err(|e| format!("could not create a window surface: {e}"))?;

        let adapter = pollster::block_on(instance.request_adapter(&wgpu::RequestAdapterOptions {
            power_preference: wgpu::PowerPreference::LowPower,
            force_fallback_adapter: false,
            compatible_surface: Some(&surface),
            apply_limit_buckets: false,
        }))
        .map_err(|e| format!("no usable GPU adapter: {e}"))?;

        let info = adapter.get_info();
        let (device, queue) = pollster::block_on(adapter.request_device(&wgpu::DeviceDescriptor {
            label: Some("mgl"),
            required_features: wgpu::Features::empty(),
            // The UI only needs small buffers and one texture per cover at a time;
            // staying inside the downlevel defaults keeps memory predictable.
            required_limits: wgpu::Limits::downlevel_defaults(),
            experimental_features: wgpu::ExperimentalFeatures::disabled(),
            memory_hints: wgpu::MemoryHints::MemoryUsage,
            trace: wgpu::Trace::Off,
        }))
        .map_err(|e| format!("could not create a GPU device: {e}"))?;

        let caps = surface.get_capabilities(&adapter);
        // Straight (non-sRGB) 8-bit formats. The UI's colours are CSS colours:
        // they are already gamma-encoded sRGB bytes, and CSS composites them in
        // that same encoded space. Writing them into an sRGB target would encode
        // them a second time (every colour washed out) and blend in linear
        // space, so the shader output is linearised explicitly instead
        // (`Rgba::to_linear`) and the target stays unorm.
        let format = caps
            .formats
            .iter()
            .copied()
            .find(|f| *f == wgpu::TextureFormat::Bgra8Unorm)
            .or_else(|| {
                caps.formats
                    .iter()
                    .copied()
                    .find(|f| *f == wgpu::TextureFormat::Rgba8Unorm)
            })
            .or_else(|| caps.formats.iter().copied().find(|f| !f.is_srgb()))
            .or_else(|| caps.formats.first().copied())
            .ok_or("the surface reported no texture formats")?;

        // Prefer a premultiplied alpha mode so the compositor blends exactly what
        // the shader produced. `Opaque` is chosen when the surface cannot do alpha,
        // and the caller is told so it can fall back to a solid background.
        let (alpha_mode, transparent) = match alpha {
            Alpha::Transparent => {
                if caps
                    .alpha_modes
                    .contains(&wgpu::CompositeAlphaMode::PreMultiplied)
                {
                    (wgpu::CompositeAlphaMode::PreMultiplied, true)
                } else if caps
                    .alpha_modes
                    .contains(&wgpu::CompositeAlphaMode::PostMultiplied)
                {
                    (wgpu::CompositeAlphaMode::PostMultiplied, true)
                } else if caps
                    .alpha_modes
                    .contains(&wgpu::CompositeAlphaMode::Inherit)
                {
                    (wgpu::CompositeAlphaMode::Inherit, true)
                } else {
                    (wgpu::CompositeAlphaMode::Opaque, false)
                }
            }
            Alpha::Opaque => (wgpu::CompositeAlphaMode::Opaque, false),
        };

        let size = window.inner_size();
        let config = wgpu::SurfaceConfiguration {
            usage: wgpu::TextureUsages::RENDER_ATTACHMENT,
            format,
            color_space: wgpu::SurfaceColorSpace::Auto,
            width: size.width.max(1),
            height: size.height.max(1),
            present_mode: wgpu::PresentMode::Fifo,
            desired_maximum_frame_latency: 2,
            alpha_mode,
            view_formats: vec![],
        };
        surface.configure(&device, &config);

        Ok(Self {
            instance,
            surface,
            device,
            queue,
            config,
            transparent,
            adapter_name: format!("{} ({:?})", info.name, info.backend),
        })
    }

    /// Resize the swapchain. Called only from `WindowEvent::Resized`/`ScaleFactorChanged`.
    pub fn resize(&mut self, width: u32, height: u32) {
        if width == 0 || height == 0 {
            // Minimised: keep the configuration, stop presenting.
            return;
        }
        if width == self.config.width && height == self.config.height {
            return;
        }
        self.config.width = width;
        self.config.height = height;
        self.surface.configure(&self.device, &self.config);
    }

    /// Acquire the next frame image, reconfiguring once if the surface was lost.
    ///
    /// Returns `None` when the frame should be skipped; the redraw that asked for
    /// it is simply not repeated, so a failing surface never spins the CPU.
    pub fn acquire(&mut self) -> Option<wgpu::SurfaceTexture> {
        use wgpu::CurrentSurfaceTexture as C;
        match self.surface.get_current_texture() {
            C::Success(frame) | C::Suboptimal(frame) => Some(frame),
            C::Outdated | C::Lost => {
                self.surface.configure(&self.device, &self.config);
                match self.surface.get_current_texture() {
                    C::Success(frame) | C::Suboptimal(frame) => Some(frame),
                    _ => None,
                }
            }
            C::Timeout | C::Occluded | C::Validation => None,
        }
    }

    /// Present an acquired frame.
    pub fn present(&self, frame: wgpu::SurfaceTexture) {
        self.queue.present(frame);
    }
}
