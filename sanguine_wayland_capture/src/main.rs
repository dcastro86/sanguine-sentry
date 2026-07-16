use std::collections::HashMap;
use std::fs;
use std::os::fd::OwnedFd;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;
use tokio::io::AsyncWriteExt;
use tokio::net::UnixListener;

use pipewire as pw;
use libspa as spa;
use spa::buffer::DataType;
use spa::param::ParamType;
use spa::param::format::{FormatProperties, MediaType, MediaSubtype};
use spa::param::video::{VideoFormat, VideoInfoRaw};
use spa::pod::{Pod, Property, Value};
use spa::utils::{Direction, Id, SpaTypes};
use pw::stream::{StreamFlags, StreamListener, StreamRc, StreamState};
use pw::main_loop::MainLoopRc;
use pw::context::ContextRc;

use zbus::blocking::{Connection, Proxy};
use zbus::zvariant::{OwnedObjectPath, OwnedValue, Value as ZValue};

#[derive(Clone)]
struct SimpleFrame {
    width: u32,
    height: u32,
    data: Vec<u8>,
    is_rgba: bool,
}

struct UserData {
    video_format: VideoInfoRaw,
    buffer_params_sent: bool,
    frame_bytes: Vec<u8>,
    active_clients: Arc<AtomicUsize>,
}

#[derive(Debug)]
enum ControlMessage {
    Start,
    Stop,
    Terminate,
}

#[repr(C)]
#[allow(non_camel_case_types)]
struct dma_buf_sync {
    flags: u64,
}

const DMA_BUF_SYNC_READ: u64 = 1;
const DMA_BUF_SYNC_START: u64 = 0;
const DMA_BUF_SYNC_END: u64 = 1 << 2;

const DMA_BUF_IOCTL_SYNC: libc::c_ulong = 0x40086200;

fn next_token() -> String {
    use std::sync::atomic::AtomicU64;
    static COUNTER: AtomicU64 = AtomicU64::new(1);
    format!("sanguine_{}", COUNTER.fetch_add(1, Ordering::Relaxed))
}

struct ScreenCastStream {
    node_id: u32,
    width: Option<u32>,
    height: Option<u32>,
}

struct ActiveScreenCast {
    fd: OwnedFd,
    streams: Vec<ScreenCastStream>,
    restore_token: Option<String>,
}

struct PortalClient {
    connection: Connection,
}

impl PortalClient {
    fn new() -> Result<Self, Box<dyn std::error::Error>> {
        let connection = Connection::session()?;
        Ok(Self { connection })
    }

    fn start_screen_cast(
        &self,
        include_cursor: bool,
        restore_token: Option<&str>,
    ) -> Result<ActiveScreenCast, Box<dyn std::error::Error>> {
        let desktop = Proxy::new(
            &self.connection,
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.ScreenCast",
        )?;

        println!("Portal: Creating screencast session...");
        let session = self.create_session(&desktop)?;
        
        println!("Portal: Selecting sources...");
        self.select_sources(&desktop, &session, include_cursor, restore_token)?;
        
        println!("Portal: Starting session...");
        let (streams, new_restore_token) = self.start(&desktop, &session)?;
        
        println!("Portal: Opening PipeWire remote...");
        let fd = self.open_pipewire_remote(&desktop, &session)?;

        Ok(ActiveScreenCast {
            fd,
            streams,
            restore_token: new_restore_token,
        })
    }

    fn create_session(&self, desktop: &Proxy<'_>) -> Result<OwnedObjectPath, Box<dyn std::error::Error>> {
        let handle_token = next_token();
        let request = self.request_proxy(&handle_token)?;
        let session_handle_token = next_token();

        let mut signal = request.receive_signal("Response")?;

        let options = HashMap::from([
            ("handle_token", ZValue::from(handle_token.as_str())),
            (
                "session_handle_token",
                ZValue::from(session_handle_token.as_str()),
            ),
        ]);

        desktop.call_method("CreateSession", &(options))?;

        let response = wait_response(&mut signal)?;
        let session_handle = response.get("session_handle").ok_or("portal response missing session_handle")?;
        let session_handle_str = String::try_from(session_handle.clone())?;
        Ok(OwnedObjectPath::try_from(session_handle_str)?)
    }

    fn select_sources(
        &self,
        desktop: &Proxy<'_>,
        session: &OwnedObjectPath,
        include_cursor: bool,
        restore_token: Option<&str>,
    ) -> Result<(), Box<dyn std::error::Error>> {
        let handle_token = next_token();
        let request = self.request_proxy(&handle_token)?;

        let mut signal = request.receive_signal("Response")?;

        let mut options = HashMap::from([
            ("handle_token", ZValue::from(handle_token.as_str())),
            ("types", ZValue::from(3_u32)), // 1 = monitor, 2 = window, 3 = both
            ("multiple", ZValue::from(false)),
            ("cursor_mode", ZValue::from(if include_cursor { 2_u32 } else { 1_u32 })), // 2 = Embedded, 1 = Hidden
            ("persist_mode", ZValue::from(2_u32)), // 2 = persistent
        ]);

        if let Some(restore_token) = restore_token {
            options.insert("restore_token", ZValue::from(restore_token));
        }

        desktop.call_method("SelectSources", &(session, options))?;
        let _ = wait_response(&mut signal)?;
        Ok(())
    }

    fn start(&self, desktop: &Proxy<'_>, session: &OwnedObjectPath) -> Result<(Vec<ScreenCastStream>, Option<String>), Box<dyn std::error::Error>> {
        let handle_token = next_token();
        let request = self.request_proxy(&handle_token)?;

        let mut signal = request.receive_signal("Response")?;

        let options = HashMap::from([("handle_token", ZValue::from(handle_token.as_str()))]);
        desktop.call_method("Start", &(session, "", options))?;

        let response = wait_response(&mut signal)?;
        let streams_val = response.get("streams").ok_or("portal response missing streams")?;
        
        let entries = Vec::<(u32, HashMap<String, OwnedValue>)>::try_from(streams_val.clone())?;
        let streams = entries.into_iter().map(|(node_id, props)| {
            let mut width = None;
            let mut height = None;
            if let Some(size_val) = props.get("size") {
                if let Ok((w, h)) = <(i32, i32)>::try_from(size_val.clone()) {
                    width = Some(w.max(0) as u32);
                    height = Some(h.max(0) as u32);
                }
            }
            ScreenCastStream { node_id, width, height }
        }).collect();

        let restore_token = response.get("restore_token")
            .map(|val| String::try_from(val.clone()))
            .transpose()?;

        Ok((streams, restore_token))
    }

    fn open_pipewire_remote(&self, desktop: &Proxy<'_>, session: &OwnedObjectPath) -> Result<OwnedFd, Box<dyn std::error::Error>> {
        let options: HashMap<&str, ZValue<'_>> = HashMap::new();
        let fd: zbus::zvariant::OwnedFd = desktop.call("OpenPipeWireRemote", &(session, options))?;
        Ok(fd.into())
    }

    fn request_proxy(&self, handle_token: &str) -> Result<Proxy<'_>, Box<dyn std::error::Error>> {
        let unique_identifier = self
            .connection
            .unique_name()
            .ok_or("missing dbus unique name")?
            .trim_start_matches(':')
            .replace('.', "_");
        let path = format!("/org/freedesktop/portal/desktop/request/{unique_identifier}/{handle_token}");
        Ok(Proxy::new(
            &self.connection,
            "org.freedesktop.portal.Desktop",
            path,
            "org.freedesktop.portal.Request",
        )?)
    }
}

fn wait_response(
    signal: &mut zbus::blocking::proxy::SignalIterator<'_>,
) -> Result<HashMap<String, OwnedValue>, Box<dyn std::error::Error>> {
    let message = signal.next().ok_or("portal response signal missing")?;
    let (code, body): (u32, HashMap<String, OwnedValue>) = message.body().deserialize()?;
    match code {
        0 => Ok(body),
        1 => Err("portal request cancelled by user".into()),
        other => Err(format!("portal request failed with response code {other}").into()),
    }
}

fn build_stream_params(
    frame_rate: u32,
    width: u32,
    height: u32,
) -> Result<Vec<Vec<u8>>, Box<dyn std::error::Error>> {
    // 1. DMA-BUF format with linear modifier
    let choice = spa::utils::Choice(
        spa::utils::ChoiceFlags::empty(),
        spa::utils::ChoiceEnum::Enum {
            default: 0_i64,
            alternatives: vec![0_i64],
        },
    );
    let mut modifier_prop = Property::new(
        FormatProperties::VideoModifier.as_raw(),
        spa::pod::Value::Choice(spa::pod::ChoiceValue::Long(choice)),
    );
    modifier_prop.flags = spa::pod::PropertyFlags::from_bits_retain(
        spa::pod::PropertyFlags::MANDATORY.bits() | (1 << 4) // (1 << 4) is DONT_FIXATE
    );

    let format_dmabuf = spa::pod::Object {
        type_: SpaTypes::ObjectParamFormat.as_raw(),
        id: ParamType::EnumFormat.as_raw(),
        properties: vec![
            spa::pod::property!(FormatProperties::MediaType, Id, MediaType::Video),
            spa::pod::property!(FormatProperties::MediaSubtype, Id, MediaSubtype::Raw),
            spa::pod::property!(
                FormatProperties::VideoFormat,
                Choice,
                Enum,
                Id,
                VideoFormat::BGRA,
                VideoFormat::BGRx,
                VideoFormat::RGBA,
                VideoFormat::RGBx,
            ),
            modifier_prop,
            spa::pod::property!(
                FormatProperties::VideoSize,
                Choice,
                Range,
                Rectangle,
                spa::utils::Rectangle { width, height },
                spa::utils::Rectangle { width: 1, height: 1 },
                spa::utils::Rectangle { width: 7680, height: 4320 }
            ),
            spa::pod::property!(
                FormatProperties::VideoFramerate,
                Choice,
                Range,
                Fraction,
                spa::utils::Fraction { num: frame_rate, denom: 1 },
                spa::utils::Fraction { num: 0, denom: 1 },
                spa::utils::Fraction { num: frame_rate, denom: 1 }
            ),
        ],
    };

    let dmabuf_bytes = spa::pod::serialize::PodSerializer::serialize(
        std::io::Cursor::new(Vec::new()),
        &spa::pod::Value::Object(format_dmabuf),
    )?
    .0
    .into_inner();

    // 2. Fallback format (without modifiers)
    let format_fallback = spa::pod::object!(
        SpaTypes::ObjectParamFormat,
        ParamType::EnumFormat,
        spa::pod::property!(FormatProperties::MediaType, Id, MediaType::Video),
        spa::pod::property!(FormatProperties::MediaSubtype, Id, MediaSubtype::Raw),
        spa::pod::property!(
            FormatProperties::VideoFormat,
            Choice,
            Enum,
            Id,
            VideoFormat::BGRA,
            VideoFormat::BGRx,
            VideoFormat::RGBA,
            VideoFormat::RGBx,
        ),
        spa::pod::property!(
            FormatProperties::VideoSize,
            Choice,
            Range,
            Rectangle,
            spa::utils::Rectangle { width, height },
            spa::utils::Rectangle { width: 1, height: 1 },
            spa::utils::Rectangle { width: 7680, height: 4320 }
        ),
        spa::pod::property!(
            FormatProperties::VideoFramerate,
            Choice,
            Range,
            Fraction,
            spa::utils::Fraction { num: frame_rate, denom: 1 },
            spa::utils::Fraction { num: 0, denom: 1 },
            spa::utils::Fraction { num: frame_rate, denom: 1 }
        ),
    );

    let fallback_bytes = spa::pod::serialize::PodSerializer::serialize(
        std::io::Cursor::new(Vec::new()),
        &spa::pod::Value::Object(format_fallback),
    )?
    .0
    .into_inner();

    Ok(vec![dmabuf_bytes, fallback_bytes])
}

fn run_video_loop(
    fd: OwnedFd,
    node_id: u32,
    runtime_active: Arc<AtomicBool>,
    latest_frame: Arc<Mutex<Option<SimpleFrame>>>,
    control_rx: std::sync::mpsc::Receiver<ControlMessage>,
    active_clients: Arc<AtomicUsize>,
    target_w: u32,
    target_h: u32,
) -> Result<(), Box<dyn std::error::Error>> {
    pw::init();

    let main_loop = MainLoopRc::new(None)?;
    let context = ContextRc::new(&main_loop, None)?;
    let core = context.connect_fd_rc(fd, None)?;

    let _core_listener = core
        .clone()
        .add_listener_local()
        .info(|info| tracing::debug!(?info, "PipeWire core info"))
        .error(|id, seq, res, message| {
            tracing::error!(id, seq, res, message, "PipeWire core error");
        })
        .done(|id, _seq| {
            tracing::trace!(id, "PipeWire core done");
        })
        .register();

    let mut stream_properties = pw::properties::PropertiesBox::new();
    stream_properties.insert(*pw::keys::MEDIA_TYPE, "Video");
    stream_properties.insert(*pw::keys::MEDIA_CATEGORY, "Capture");
    stream_properties.insert(*pw::keys::MEDIA_ROLE, "Screen");

    let stream = StreamRc::new(core, "sanguine-dma-video", stream_properties)?;

    let listener = stream
        .add_local_listener_with_user_data(UserData {
            video_format: VideoInfoRaw::default(),
            buffer_params_sent: false,
            frame_bytes: Vec::new(),
            active_clients,
        })
        .state_changed(|_, _, _, new| match new {
            StreamState::Error(msg) => {
                tracing::error!(error = %msg, "PipeWire stream entered error state");
            }
            StreamState::Unconnected => tracing::debug!("PipeWire stream: unconnected"),
            StreamState::Connecting => tracing::debug!("PipeWire stream: connecting"),
            StreamState::Paused => tracing::debug!("PipeWire stream: paused"),
            StreamState::Streaming => tracing::debug!("PipeWire stream: streaming"),
        })
        .param_changed(|_, user_data, id, param| {
            let Some(param) = param else { return; };
            if id != ParamType::Format.as_raw() { return; }
            let Ok((media_type, media_subtype)) = pw::spa::param::format_utils::parse_format(param) else { return; };
            if media_type != MediaType::Video || media_subtype != MediaSubtype::Raw { return; }
            if let Err(e) = user_data.video_format.parse(param) {
                tracing::warn!(error = %e, "PipeWire stream format parse failed");
                return;
            }
            user_data.buffer_params_sent = true;
        })
        .process({
            let runtime = Arc::clone(&runtime_active);
            let latest_frame = Arc::clone(&latest_frame);
            let first_frame = Arc::new(AtomicBool::new(true));
            move |stream, user_data| {
                if !runtime.load(Ordering::Relaxed) {
                    return;
                }

                if user_data.active_clients.load(Ordering::Relaxed) == 0 {
                    return;
                }

                let Some(mut buffer) = stream.dequeue_buffer() else {
                    return;
                };

                let datas = buffer.datas_mut();
                if datas.is_empty() {
                    return;
                }

                let data = &mut datas[0];
                let size = user_data.video_format.size();
                let frame_w = size.width as usize;
                let frame_h = size.height as usize;
                let chunk = data.chunk();
                let stride = chunk.stride().max(0) as usize;
                let offset = chunk.offset() as usize;
                let chunk_size = chunk.size() as usize;

                let data_type = data.type_();
                if first_frame.swap(false, Ordering::Relaxed) {
                    println!("First frame received! DataType: {:?}", data_type);
                }
                
                if user_data.frame_bytes.len() != frame_w * frame_h * 4 {
                    user_data.frame_bytes = vec![0u8; frame_w * frame_h * 4];
                }
                let mut success = false;

                if data_type == DataType::DmaBuf {
                    let fd = data.fd();
                    let maxsize = data.as_raw().maxsize as usize;

                    unsafe {
                        let ptr = libc::mmap(
                            std::ptr::null_mut(),
                            maxsize,
                            libc::PROT_READ,
                            libc::MAP_SHARED,
                            fd,
                            0,
                        );
                        if ptr != libc::MAP_FAILED {
                            let mut sync_start = dma_buf_sync {
                                flags: DMA_BUF_SYNC_START | DMA_BUF_SYNC_READ,
                            };
                            libc::ioctl(fd, DMA_BUF_IOCTL_SYNC, &mut sync_start);

                             let bytes_per_pixel = 4;
                             let row_bytes = frame_w * bytes_per_pixel;
                             let actual_stride = if stride > 0 { stride } else { row_bytes };

                             let needed_size = (frame_h - 1) * actual_stride + row_bytes;
                             if offset + needed_size <= maxsize {
                                 let src_ptr = (ptr as *const u8).add(offset);
                                 for row in 0..frame_h {
                                     let src_row = src_ptr.add(row * actual_stride);
                                     let dest_row = user_data.frame_bytes.as_mut_ptr().add(row * row_bytes);
                                     std::ptr::copy_nonoverlapping(src_row, dest_row, row_bytes);
                                 }
                                 success = true;
                             } else {
                                 tracing::error!("DMA-BUF dimensions exceed maxsize: offset={}, needed_size={}, maxsize={}", offset, needed_size, maxsize);
                             }

                            let mut sync_end = dma_buf_sync {
                                flags: DMA_BUF_SYNC_END | DMA_BUF_SYNC_READ,
                            };
                            libc::ioctl(fd, DMA_BUF_IOCTL_SYNC, &mut sync_end);
                            libc::munmap(ptr, maxsize);
                        } else {
                            tracing::error!("mmap failed on DMA-BUF fd");
                        }
                    }
                } else if let Some(raw) = data.data() {
                    if offset + chunk_size <= raw.len() {
                        let src = &raw[offset..offset + chunk_size];
                        let bytes_per_pixel = 4;
                        let row_bytes = frame_w * bytes_per_pixel;
                        let actual_stride = if stride > 0 { stride } else { row_bytes };

                        for row in 0..frame_h {
                            let start = row * actual_stride;
                            if start + row_bytes <= src.len() {
                                let dest_start = row * row_bytes;
                                user_data.frame_bytes[dest_start..dest_start + row_bytes].copy_from_slice(&src[start..start + row_bytes]);
                            }
                        }
                        success = true;
                    }
                }

                if success {
                    let format = user_data.video_format.format();
                    let is_rgba = format == VideoFormat::RGBA || format == VideoFormat::RGBx;

                     let mut lock = match latest_frame.lock() {
                         Ok(l) => l,
                         Err(p) => p.into_inner(),
                     };
                    *lock = Some(SimpleFrame {
                        width: size.width,
                        height: size.height,
                        data: user_data.frame_bytes.clone(),
                        is_rgba,
                    });
                }
            }
        })
        .register()?;

    let _listener: StreamListener<UserData> = listener;

    let connect_params = build_stream_params(60, target_w, target_h)?;

    let metas_obj = spa::pod::object!(
        SpaTypes::ObjectParamMeta,
        ParamType::Meta,
        Property::new(
            spa::sys::SPA_PARAM_META_type,
            Value::Id(Id(spa::sys::SPA_META_Header))
        ),
        Property::new(
            spa::sys::SPA_PARAM_META_size,
            Value::Int(std::mem::size_of::<spa::sys::spa_meta_header>() as i32)
        ),
    );
    let metas_bytes = spa::pod::serialize::PodSerializer::serialize(
        std::io::Cursor::new(Vec::new()),
        &Value::Object(metas_obj),
    )?
    .0
    .into_inner();

    let mut params = connect_params
        .iter()
        .filter_map(|bytes| Pod::from_bytes(bytes))
        .chain(Pod::from_bytes(&metas_bytes))
        .collect::<Vec<_>>();

    stream.connect(
        Direction::Input,
        Some(node_id),
        StreamFlags::AUTOCONNECT | StreamFlags::MAP_BUFFERS | StreamFlags::RT_PROCESS,
        &mut params,
    )?;

    let pw_loop = main_loop.loop_();
    let mut terminate = false;
    while !terminate {
        while let Ok(message) = control_rx.try_recv() {
            match message {
                ControlMessage::Start => {
                    runtime_active.store(true, Ordering::Relaxed);
                }
                ControlMessage::Stop => {
                    runtime_active.store(false, Ordering::Relaxed);
                }
                ControlMessage::Terminate => {
                    terminate = true;
                }
            }
        }

        pw_loop.iterate(pw::loop_::Timeout::Finite(Duration::from_millis(20)));
    }

    Ok(())
}

fn get_socket_path() -> std::path::PathBuf {
    if let Ok(runtime_dir) = std::env::var("XDG_RUNTIME_DIR") {
        let path = std::path::Path::new(&runtime_dir);
        if path.is_dir() {
            return path.join("sanguine_sentry.sock");
        }
    }
    if let Ok(home) = std::env::var("HOME") {
        return std::path::Path::new(&home).join(".sanguine_sentry.sock");
    }
    std::path::PathBuf::from("/tmp/sanguine_sentry.sock")
}

fn get_restore_token_path() -> std::path::PathBuf {
    if let Ok(runtime_dir) = std::env::var("XDG_RUNTIME_DIR") {
        let path = std::path::Path::new(&runtime_dir);
        if path.is_dir() {
            return path.join("sanguine_restore_token.txt");
        }
    }
    if let Ok(home) = std::env::var("HOME") {
        return std::path::Path::new(&home).join(".sanguine_restore_token.txt");
    }
    std::path::PathBuf::from("/tmp/sanguine_restore_token.txt")
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt::init();
    println!("Initializing Sanguine Sentry Wayland daemon (PipeWire & DMA-BUF)...");

    let portal_client = PortalClient::new()?;

    let token_path = get_restore_token_path();
    let saved_token = std::fs::read_to_string(&token_path)
        .ok()
        .map(|t| t.trim().to_string());

    let cast = match portal_client.start_screen_cast(true, saved_token.as_deref()) {
        Ok(c) => c,
        Err(e) => {
            println!("Portal request with token failed: {:?}. Retrying without token...", e);
            let _ = std::fs::remove_file(&token_path);
            portal_client.start_screen_cast(true, None)?
        }
    };

    if let Some(ref token) = cast.restore_token {
        println!("Saving restore token to bypass future dialogs: {}", token);
        let token_path = get_restore_token_path();
        
        use std::os::unix::fs::OpenOptionsExt;
        let mut options = fs::OpenOptions::new();
        options.write(true).create(true).truncate(true).mode(0o600);
        
        if let Ok(mut file) = options.open(&token_path) {
            use std::io::Write;
            let _ = file.write_all(token.as_bytes());
        }
    }

    let stream = cast.streams.into_iter().next().ok_or("No screen cast streams returned by portal")?;
    let node_id = stream.node_id;
    let fd = cast.fd;

    let latest_frame = Arc::new(Mutex::new(None::<SimpleFrame>));
    let (control_tx, control_rx) = std::sync::mpsc::channel();
    let runtime_active = Arc::new(AtomicBool::new(true));
    let active_clients = Arc::new(AtomicUsize::new(0));

    let pw_latest_frame = latest_frame.clone();
    let pw_runtime_active = runtime_active.clone();
    let pw_active_clients = active_clients.clone();
    
    let target_w = stream.width.unwrap_or(1920);
    let target_h = stream.height.unwrap_or(1080);
    println!("Dynamic display size reported by portal: {}x{}", target_w, target_h);

    // Spawn PipeWire loop in background thread
    let worker_handle = thread::spawn(move || {
        if let Err(e) = run_video_loop(
            fd,
            node_id,
            pw_runtime_active,
            pw_latest_frame,
            control_rx,
            pw_active_clients,
            target_w,
            target_h,
        ) {
            eprintln!("PipeWire worker error: {:?}", e);
        }
    });

    // Setup UNIX domain socket listener
    let socket_path = get_socket_path();
    if std::fs::metadata(&socket_path).is_ok() {
        let _ = std::fs::remove_file(&socket_path);
    }

    let old_umask = unsafe { libc::umask(0o177) };
    let listener = UnixListener::bind(&socket_path);
    unsafe { libc::umask(old_umask) };
    let listener = listener?;

    println!("UNIX socket listening at: {}", socket_path.display());

    let ctrl_tx = control_tx.clone();
    tokio::spawn(async move {
        tokio::signal::ctrl_c().await.ok();
        println!("Shutting down capture daemon...");
        let _ = ctrl_tx.send(ControlMessage::Terminate);
    });

    loop {
        let (socket, _) = match listener.accept().await {
            Ok(val) => val,
            Err(_) => break,
        };

        // Peer UID validation to prevent local privilege escalation / cross-user spoofing
        if let Ok(cred) = socket.peer_cred() {
            let current_uid = unsafe { libc::getuid() };
            if cred.uid() != current_uid {
                eprintln!("Rejected socket connection from unauthorized UID: {}", cred.uid());
                continue;
            }
        } else {
            eprintln!("Failed to read peer credentials, rejecting socket connection");
            continue;
        }

        let frame_ref = latest_frame.clone();
        let client_count_clone = active_clients.clone();

        tokio::spawn(async move {
            struct ClientGuard(Arc<AtomicUsize>);
            impl Drop for ClientGuard {
                fn drop(&mut self) {
                    self.0.fetch_sub(1, Ordering::SeqCst);
                }
            }
            client_count_clone.fetch_add(1, Ordering::SeqCst);
            let _guard = ClientGuard(client_count_clone);

            use tokio::io::AsyncBufReadExt;
            let mut reader = tokio::io::BufReader::new(socket);
            let mut line = String::new();
            loop {
                line.clear();
                match reader.read_line(&mut line).await {
                    Ok(0) => break, // EOF
                    Ok(_) => {
                        let parts: Vec<&str> = line.trim().split_whitespace().collect();
                        if parts.len() != 4 {
                            let socket_ref = reader.get_mut();
                            let _ = socket_ref.write_all(b"ERROR: Invalid request format. Use 'x y w h'\n").await;
                            continue;
                        }

                        let crop_x: usize = parts[0].parse().unwrap_or(0);
                        let crop_y: usize = parts[1].parse().unwrap_or(0);
                        let crop_w: usize = parts[2].parse().unwrap_or(10);
                        let crop_h: usize = parts[3].parse().unwrap_or(10);

                        let opt_frame = match frame_ref.lock() {
                            Ok(lock) => lock.clone(),
                            Err(poisoned) => poisoned.into_inner().clone(),
                        };

                        if let Some(frame) = opt_frame {
                            let frame_w = frame.width as usize;
                            let frame_h = frame.height as usize;

                            let fits = crop_x
                                .checked_add(crop_w)
                                .map(|sum| sum <= frame_w)
                                .unwrap_or(false)
                                && crop_y
                                    .checked_add(crop_h)
                                    .map(|sum| sum <= frame_h)
                                    .unwrap_or(false);

                            if fits {
                                let is_rgba = frame.is_rgba;
                                let mut rgb_data = Vec::with_capacity(crop_w * crop_h * 3);
                                for y in crop_y..(crop_y + crop_h) {
                                    for x in crop_x..(crop_x + crop_w) {
                                        let idx = (y * frame_w + x) * 4;
                                        if is_rgba {
                                            rgb_data.push(frame.data[idx]);     // R
                                            rgb_data.push(frame.data[idx + 1]); // G
                                            rgb_data.push(frame.data[idx + 2]); // B
                                        } else {
                                            // BGRA
                                            rgb_data.push(frame.data[idx + 2]); // R
                                            rgb_data.push(frame.data[idx + 1]); // G
                                            rgb_data.push(frame.data[idx]);     // B
                                        }
                                    }
                                }

                                let header = format!("OK {} {}\n", crop_w, crop_h);
                                let socket_ref = reader.get_mut();
                                if socket_ref.write_all(header.as_bytes()).await.is_err() { break; }
                                if socket_ref.write_all(&rgb_data).await.is_err() { break; }
                            } else {
                                let socket_ref = reader.get_mut();
                                let _ = socket_ref.write_all(b"ERROR: Coordinates out of bounds\n").await;
                            }
                        } else {
                            let socket_ref = reader.get_mut();
                            let _ = socket_ref.write_all(b"ERROR: No frame captured yet\n").await;
                        }
                    }
                    Err(_) => break,
                }
            }
        });
    }

    let _ = worker_handle.join();
    Ok(())
}
