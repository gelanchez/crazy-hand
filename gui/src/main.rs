use eframe::egui::{self, Color32, RichText};

const GUI_NAME: &str = "Crazyflie GUI";
const IMAGE_WIDTH: usize = 324;
const IMAGE_HEIGHT: usize = 244;
const WINDOW_WIDTH: f32 = IMAGE_WIDTH as f32 + 215.0;
const WINDOW_HEIGHT: f32 = IMAGE_HEIGHT as f32 + 40.0;

fn main() -> eframe::Result {
    let nataive_options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([WINDOW_WIDTH, WINDOW_HEIGHT])
            .with_min_inner_size([WINDOW_WIDTH, WINDOW_HEIGHT]),
        ..Default::default()
    };
    eframe::run_native(
        GUI_NAME,
        nataive_options,
        Box::new(|cc| {
            // This gives us image support:
            egui_extras::install_image_loaders(&cc.egui_ctx);
            Ok(Box::new(Gui {
                show_about: false,
                save_images: false,
                is_connected: false,
                battery: 0.0,
            }))
        }),
    )
}

struct Gui {
    show_about: bool,
    save_images: bool,
    is_connected: bool,
    battery: f32,
}

impl eframe::App for Gui {
    // TODO: Read from iceoryx2 SHM
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        egui::Panel::top("menu_bar").show_inside(ui, |ui| {
            egui::MenuBar::new().ui(ui, |ui| {
                ui.menu_button("App", |ui| {
                    if ui.button("❌ Quit").clicked() {
                        std::process::exit(0);
                    }
                });

                // We use the explicit MenuButton builder to override the default closing behavior.
                // This keeps the menu open when clicking the checkbox, as by default menus close
                // on any interaction. We use egui::containers::menu as egui::menu is deprecated.
                egui::containers::menu::MenuButton::new("Options")
                    .config(
                        egui::containers::menu::MenuConfig::new()
                            .close_behavior(egui::PopupCloseBehavior::CloseOnClickOutside),
                    )
                    .ui(ui, |ui| {
                        ui.checkbox(&mut self.save_images, "💾 Save images")
                            .on_hover_text("Save images to disk");
                    });

                ui.menu_button("Help", |ui| {
                    if ui.button(format!("❓ About {}", GUI_NAME)).clicked() {
                        self.show_about = true;
                        ui.close();
                    }
                });
            });
        });

        egui::Panel::right("right_panel")
            .resizable(false)
            .show_inside(ui, |ui| {
                // DRONE CONTROL
                ui.vertical_centered_justified(|ui| {
                    if ui
                        .button(RichText::new("Take off ⬆").color(Color32::LIGHT_GREEN))
                        .on_hover_text("Take off the drone")
                        .clicked()
                    {
                        // TODO: TAKE-OFF
                    }
                    if ui
                        .button(RichText::new("Land ⬇").color(Color32::LIGHT_RED))
                        .on_hover_text("Land the drone")
                        .clicked()
                    {
                        // TODO LAND
                    }
                });
                ui.separator();

                // TELEMETRY
                let (text, color) = if self.is_connected {
                    ("CONNECTED", Color32::GREEN)
                } else {
                    ("NOT CONNECTED", Color32::RED)
                };
                ui.colored_label(color, text);
                ui.horizontal(|ui| {
                    ui.label("Battery:");
                    ui.add(
                        egui::ProgressBar::new(self.battery / 100.0)
                            .text(format!("{:.1}%", self.battery))
                            .corner_radius(1.0),
                    );
                });
            });

        egui::CentralPanel::default().show_inside(ui, |ui| {
            ui.centered_and_justified(|ui| {
                ui.label("No image");
            });
        });

        if self.show_about {
            egui::Window::new(format!("About {}", GUI_NAME))
                .open(&mut self.show_about)
                .pivot(egui::Align2::CENTER_CENTER)
                .resizable(false)
                .collapsible(false)
                .show(ui.ctx(), |ui| {
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
}
