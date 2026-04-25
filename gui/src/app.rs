use eframe::egui;
use egui::{Color32, TextureHandle, TextureOptions};
use shared::{Command, IMAGE_HEIGHT, IMAGE_WIDTH, Telemetry};
use std::sync::{Arc, Mutex};
use std::time::Instant;

pub const GUI_NAME: &str = "crazyflie-gui";

/// Shared state between the GUI and the IPC thread.
pub struct GuiData {
    pub image: Option<Vec<u8>>,
    pub telemetry: Telemetry,
    pub last_telemetry_time: Instant,
    pub last_image_time: Instant,
    pub command: Option<Command>,
}

impl Default for GuiData {
    fn default() -> Self {
        Self {
            image: None,
            telemetry: Telemetry::default(),
            last_telemetry_time: Instant::now(),
            last_image_time: Instant::now(),
            command: None,
        }
    }
}

pub struct App {
    gui_data: Arc<Mutex<GuiData>>,
    /// GPU texture, updated whenever a new frame arrives.
    texture: Option<TextureHandle>,
    /// Local copies.
    show_about: bool,
    show_shortcuts: bool,
    show_config: bool,
    save_images: bool,
    telemetry: Telemetry,
    image_connected: bool,
    thrust: f32,
    pitch: f32,
    roll: f32,
    yaw: f32,
    is_flying: bool,
    command_counter: u64,
    base_sensitivity: f32,
    turbo_sensitivity: f32,
}

impl App {
    pub fn new(gui_data: Arc<Mutex<GuiData>>) -> Self {
        Self {
            gui_data,
            texture: None,
            show_about: false,
            show_shortcuts: false,
            show_config: false,
            save_images: false,
            telemetry: Telemetry::default(),
            image_connected: false,
            thrust: 0.0,
            pitch: 0.0,
            roll: 0.0,
            yaw: 0.0,
            is_flying: false,
            command_counter: 0,
            base_sensitivity: 0.4,
            turbo_sensitivity: 1.0,
        }
    }

    fn send_command(&self, cmd: Command) {
        self.gui_data.lock().unwrap().command = Some(cmd);
    }

    fn top_bar(&mut self, ui: &mut egui::Ui) {
        egui::Panel::top("menu_bar").show_inside(ui, |ui| {
            egui::MenuBar::new().ui(ui, |ui| {
                ui.menu_button("App", |ui| {
                    if ui
                        .add(egui::Button::new("❌ Quit").shortcut_text("Ctrl+Q"))
                        .clicked()
                    {
                        std::process::exit(0);
                    }
                });

                egui::containers::menu::MenuButton::new("Options")
                    .config(
                        egui::containers::menu::MenuConfig::new()
                            .close_behavior(egui::PopupCloseBehavior::CloseOnClickOutside),
                    )
                    .ui(ui, |ui| {
                        ui.checkbox(&mut self.save_images, "💾 Save images")
                            .on_hover_text("Save images to disk");

                        if ui.button("⚙ Settings").clicked() {
                            self.show_config = true;
                            ui.close();
                        }
                    });

                ui.menu_button("Help", |ui| {
                    if ui.button("⌨ Keyboard shortcuts").clicked() {
                        self.show_shortcuts = true;
                        ui.close();
                    }
                    if ui.button("ℹ About").clicked() {
                        self.show_about = true;
                        ui.close();
                    }
                });
            });
        });
    }

    fn right_panel(&mut self, ui: &mut egui::Ui) {
        egui::Panel::right("right_panel")
            .default_size(220.0)
            .resizable(false)
            .show_inside(ui, |ui| {
                ui.vertical_centered(|ui| {
                    ui.add_space(4.0);

                    // EMERGENCY STOP
                    if ui
                        .add_sized(
                            [ui.available_width(), 30.0],
                            egui::Button::new("🚨 EMERGENCY STOP 🚨")
                                .fill(Color32::from_rgb(180, 0, 0))
                                .corner_radius(4.0),
                        )
                        .on_hover_text("CUT POWER IMMEDIATELY (Backspace)")
                        .clicked()
                    {
                        self.send_command(Command {
                            action: shared::Action::EmergencyStop,
                            ..Default::default()
                        });
                        self.is_flying = false;
                    }

                    ui.add_space(8.0);

                    ui.horizontal(|ui| {
                        let btn_width = (ui.available_width() - ui.spacing().item_spacing.x) / 2.0;
                        // TAKE-OFF
                        if ui
                            .add_sized([btn_width, 30.0], egui::Button::new("Take off ⬆"))
                            .on_hover_text("Take off the drone")
                            .clicked()
                        {
                            self.send_command(Command {
                                action: shared::Action::Takeoff,
                                ..Default::default()
                            });
                            self.is_flying = true;
                        }
                        // LAND
                        if ui
                            .add_sized([btn_width, 30.0], egui::Button::new("Land ⬇"))
                            .on_hover_text("Land the drone")
                            .clicked()
                        {
                            self.send_command(Command {
                                action: shared::Action::Land,
                                ..Default::default()
                            });
                            self.is_flying = false;
                        }
                    });

                    ui.add_space(10.0);

                    ui.add(
                        egui::Slider::new(&mut self.thrust, 0.0..=1.0)
                            .text("Thrust")
                            .max_decimals(1),
                    );
                    ui.add(
                        egui::Slider::new(&mut self.pitch, -1.0..=1.0)
                            .text("Pitch")
                            .max_decimals(1),
                    );
                    ui.add(
                        egui::Slider::new(&mut self.roll, -1.0..=1.0)
                            .text("Roll")
                            .max_decimals(1),
                    );
                    ui.add(
                        egui::Slider::new(&mut self.yaw, -1.0..=1.0)
                            .text("Yaw")
                            .max_decimals(1),
                    );
                });

                ui.add_space(10.0);
                ui.separator();
                ui.add_space(5.0);

                ui.vertical(|ui| {
                    // LINK STATUS
                    ui.with_layout(egui::Layout::top_down(egui::Align::Center), |ui| {
                        let (c_text, c_color) = if self.telemetry.connected {
                            ("CONTROL LINK: OK", Color32::GREEN)
                        } else {
                            ("CONTROL LINK: LOST", Color32::RED)
                        };
                        ui.colored_label(c_color, c_text);

                        let (v_text, v_color) = if self.image_connected {
                            ("VIDEO LINK: OK", Color32::GREEN)
                        } else {
                            ("VIDEO LINK: LOST", Color32::RED)
                        };
                        ui.colored_label(v_color, v_text);
                    });

                    ui.add_space(10.0);

                    // TELEMETRY
                    let t = if self.telemetry.connected {
                        self.telemetry
                    } else {
                        Telemetry::default()
                    };

                    ui.horizontal(|ui| {
                        ui.label("Battery:");
                        ui.add(
                            egui::ProgressBar::new(t.battery_percentage / 100.0)
                                .text(format!("{:.1}%", t.battery_percentage))
                                .corner_radius(1.0),
                        );
                    });
                    ui.add_space(5.0);
                    ui.label(format!("Voltage: {:.2} V", t.battery_voltage));
                    ui.label(format!("Signal: {:.0} dBm", t.rssi));
                });
            });
    }

    fn central_panel(&mut self, ui: &mut egui::Ui) {
        egui::CentralPanel::default().show_inside(ui, |ui| {
            ui.centered_and_justified(|ui| {
                if self.image_connected {
                    match &self.texture {
                        Some(tex) => {
                            // Render at original 1:1 size to avoid stretching
                            ui.image((
                                tex.id(),
                                egui::vec2(IMAGE_WIDTH as f32, IMAGE_HEIGHT as f32),
                            ));
                        }
                        None => {
                            ui.label("Waiting for camera stream…");
                        }
                    }
                } else {
                    ui.label("Drone Disconnected");
                }
            });
        });
    }

    fn show_about_window(&mut self, ctx: &egui::Context) {
        if self.show_about {
            egui::Window::new(format!("About {}", GUI_NAME))
                .open(&mut self.show_about)
                .pivot(egui::Align2::CENTER_CENTER)
                .resizable(false)
                .collapsible(false)
                .show(ctx, |ui| {
                    ui.with_layout(egui::Layout::top_down(egui::Align::Center), |ui| {
                        ui.heading(GUI_NAME);
                        ui.label(format!("Version {}", env!("CARGO_PKG_VERSION")));
                        ui.label(env!("CARGO_PKG_DESCRIPTION"));
                        ui.separator();
                        ui.label(format!("Author: {}", env!("CARGO_PKG_AUTHORS")));
                        ui.hyperlink(env!("CARGO_PKG_REPOSITORY"));
                    });
                });
        }
    }

    fn show_shortcuts_window(&mut self, ctx: &egui::Context) {
        if self.show_shortcuts {
            egui::Window::new("⌨ Keyboard Shortcuts")
                .open(&mut self.show_shortcuts)
                .pivot(egui::Align2::CENTER_CENTER)
                .resizable(false)
                .collapsible(false)
                .show(ctx, |ui| {
                    egui::Grid::new("shortcuts_grid")
                        .striped(true)
                        .spacing([40.0, 8.0])
                        .show(ui, |ui| {
                            ui.label(egui::RichText::new("Action").strong());
                            ui.label(egui::RichText::new("Shortcut").strong());
                            ui.end_row();

                            ui.label("Quit Application");
                            ui.label("Ctrl + Q");
                            ui.end_row();

                            ui.label("Pitch Forward / Backward");
                            ui.label("W / S");
                            ui.end_row();

                            ui.label("Roll Left / Right");
                            ui.label("A / D");
                            ui.end_row();

                            ui.label("Yaw Left / Right");
                            ui.label("Q / E  or  ⬅ / ➡ Arrows");
                            ui.end_row();

                            ui.label("Thrust Up / Down");
                            ui.label("⬆ / ⬇ Arrows");
                            ui.end_row();

                            ui.label("Take-off / Land");
                            ui.label("Spacebar");
                            ui.end_row();

                            ui.label("Emergency Stop");
                            ui.label("Backspace");
                            ui.end_row();

                            ui.label("High Sensitivity");
                            ui.label("Hold SHIFT");
                            ui.end_row();
                        });
                });
        }
    }

    fn handle_keys(&mut self, ui: &egui::Ui) {
        ui.input(|i| {
            // Sensitivity multiplier
            let sensitivity = if i.modifiers.shift {
                self.turbo_sensitivity
            } else {
                self.base_sensitivity
            };

            // WASD -> Pitch & Roll
            // Pitch (Forward/Backward)
            if i.key_down(egui::Key::W) {
                self.pitch = sensitivity;
            } else if i.key_down(egui::Key::S) {
                self.pitch = -sensitivity;
            } else {
                self.pitch = 0.0;
            }

            // Roll (Left/Right)
            if i.key_down(egui::Key::D) {
                self.roll = sensitivity;
            } else if i.key_down(egui::Key::A) {
                self.roll = -sensitivity;
            } else {
                self.roll = 0.0;
            }

            // Arrow Keys -> Yaw & Thrust
            // Yaw (Rotation)
            if i.key_down(egui::Key::ArrowRight) || i.key_down(egui::Key::E) {
                self.yaw = sensitivity;
            } else if i.key_down(egui::Key::ArrowLeft) || i.key_down(egui::Key::Q) {
                self.yaw = -sensitivity;
            } else {
                self.yaw = 0.0;
            }

            // Thrust (Up/Down)
            // Note: Thrust is 0..1, so we use a neutral "hover" point of 0.5 for keyboard control
            if i.key_down(egui::Key::ArrowUp) {
                self.thrust = 0.5 + (sensitivity * 0.5);
            } else if i.key_down(egui::Key::ArrowDown) {
                self.thrust = 0.5 - (sensitivity * 0.5);
            } else {
                self.thrust = 0.0; // In a real drone, you'd likely maintain hover thrust
            }

            // Spacebar -> Toggle Takeoff / Land
            if i.key_pressed(egui::Key::Space) {
                if !self.is_flying {
                    self.send_command(Command {
                        action: shared::Action::Takeoff,
                        ..Default::default()
                    });
                    self.is_flying = true;
                } else {
                    self.send_command(Command {
                        action: shared::Action::Land,
                        ..Default::default()
                    });
                    self.is_flying = false;
                }
            }

            // Backspace -> Emergency Stop
            if i.key_pressed(egui::Key::Backspace) {
                self.send_command(Command {
                    action: shared::Action::EmergencyStop,
                    ..Default::default()
                });
                self.is_flying = false;
            }
        });
    }

    fn show_config_window(&mut self, ctx: &egui::Context) {
        if self.show_config {
            egui::Window::new("⚙ Settings")
                .open(&mut self.show_config)
                .pivot(egui::Align2::CENTER_CENTER)
                .resizable(false)
                .collapsible(false)
                .show(ctx, |ui| {
                    ui.add(
                        egui::Slider::new(&mut self.base_sensitivity, 0.1..=1.0)
                            .text("Base Sens.")
                            .max_decimals(1),
                    );
                    ui.add(
                        egui::Slider::new(&mut self.turbo_sensitivity, 0.1..=1.0)
                            .text("Turbo Sens.")
                            .max_decimals(1),
                    );
                });
        }
    }
}

impl eframe::App for App {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        // Global shortcuts
        if ui.input_mut(|i| {
            i.consume_shortcut(&egui::KeyboardShortcut::new(
                egui::Modifiers::CTRL,
                egui::Key::Q,
            ))
        }) {
            std::process::exit(0);
        }

        // Texture Update logic
        let image_data = {
            let mut data = self.gui_data.lock().unwrap();
            if let Some(pixels) = data.image.take() {
                self.image_connected = true;
                self.telemetry = data.telemetry;
                Some(pixels)
            } else {
                None
            }
        };

        if let Some(pixels) = image_data {
            let color_image = egui::ColorImage::from_gray([IMAGE_WIDTH, IMAGE_HEIGHT], &pixels);

            if let Some(tex) = &mut self.texture {
                tex.set(color_image, TextureOptions::NEAREST);
            } else {
                self.texture = Some(ui.ctx().load_texture(
                    "camera_stream",
                    color_image,
                    TextureOptions::NEAREST,
                ));
            }
        }

        self.top_bar(ui);
        self.right_panel(ui);
        self.central_panel(ui);

        self.show_about_window(ui.ctx());
        self.show_shortcuts_window(ui.ctx());
        self.show_config_window(ui.ctx());
        self.handle_keys(ui);

        // Continuous Command Send
        let any_key = ui.input(|i| {
            i.key_down(egui::Key::W)
                || i.key_down(egui::Key::S)
                || i.key_down(egui::Key::A)
                || i.key_down(egui::Key::D)
                || i.key_down(egui::Key::Q)
                || i.key_down(egui::Key::E)
                || i.key_down(egui::Key::ArrowUp)
                || i.key_down(egui::Key::ArrowDown)
                || i.key_down(egui::Key::ArrowLeft)
                || i.key_down(egui::Key::ArrowRight)
        });

        if any_key {
            self.command_counter += 1;
            self.send_command(Command {
                id: self.command_counter,
                timestamp: std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_millis() as u64,
                thrust: self.thrust,
                pitch: self.pitch,
                roll: self.roll,
                yaw: self.yaw,
                action: shared::Action::None,
                ..Default::default()
            });
            ui.ctx().request_repaint();
        }
    }
}
