use async_trait::async_trait;
use byteorder::{LittleEndian, WriteBytesExt};
use crazyflie_link::{ConnectionStatus, ConnectionTrait, Packet, RadioLinkStatistics};
use crazyflie_link::error::{Error as LinkError, Result as LinkResult};
use std::sync::Arc;
use flume::{Receiver, Sender};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::Mutex;
use tracing::info;

pub const CPX_T_STM32: u8 = 0x01;
pub const CPX_T_ESP32: u8 = 0x02;
pub const CPX_T_WIFI_HOST: u8 = 0x03;
pub const CPX_T_GAP8: u8 = 0x04;

pub const CPX_F_SYSTEM: u8 = 0x01;
pub const CPX_F_CONSOLE: u8 = 0x02;
pub const CPX_F_CRTP: u8 = 0x03;
pub const CPX_F_WIFI_CTRL: u8 = 0x04;
pub const CPX_F_APP: u8 = 0x05;

pub struct WifiLink {
    status: Arc<Mutex<ConnectionStatus>>,
    crtp_rx: Receiver<Packet>,
    crtp_tx: Sender<Packet>,
    close_tx: Sender<()>,
}

impl WifiLink {
    pub async fn connect(addr: &str, image_tx: Sender<Vec<u8>>) -> Result<Self, Box<dyn std::error::Error>> {
        tracing::info!("Connecting to WiFi Link at {}...", addr);
        let stream = TcpStream::connect(addr).await?;
        let (mut reader, writer) = stream.into_split();

        let (crtp_tx_out, crtp_rx_in) = flume::unbounded::<Packet>();
        let (crtp_tx_in, crtp_rx_out) = flume::unbounded::<Packet>();
        let (close_tx, close_rx) = flume::unbounded::<()>();

        let status = Arc::new(Mutex::new(ConnectionStatus::Connected));
        let status_clone_init = status.clone();
        let crtp_rx_in_init = crtp_rx_in;
        let close_rx_init = close_rx;
        let mut writer_init = writer;
        tokio::spawn(async move {
            tracing::info!("Sending CPX initialization sequence...");
            // 1. Enable CRTP bridge: Function SYSTEM(1), Data [0x21, 0x01]
            {
                let data = vec![0x21, 0x01];
                let length = (data.len() + 2) as u16;
                let route_byte = (0 << 7) | (0 << 6) | (CPX_T_WIFI_HOST << 3) | CPX_T_STM32;
                let func_byte = (0 << 6) | CPX_F_SYSTEM;
                let mut buf = Vec::new();
                let _ = <Vec<u8> as WriteBytesExt>::write_u16::<LittleEndian>(&mut buf, length);
                buf.push(route_byte);
                buf.push(func_byte);
                buf.extend_from_slice(&data);
                let _ = writer_init.write_all(&buf).await;
            }
            // 2. Set client connected: Function SYSTEM(1), Data [0x20, 0x01]
            {
                let data = vec![0x20, 0x01];
                let length = (data.len() + 2) as u16;
                let route_byte = (0 << 7) | (0 << 6) | (CPX_T_WIFI_HOST << 3) | CPX_T_STM32;
                let func_byte = (0 << 6) | CPX_F_SYSTEM;
                let mut buf = Vec::new();
                let _ = <Vec<u8> as WriteBytesExt>::write_u16::<LittleEndian>(&mut buf, length);
                buf.push(route_byte);
                buf.push(func_byte);
                buf.extend_from_slice(&data);
                let _ = writer_init.write_all(&buf).await;
            }
            tokio::time::sleep(tokio::time::Duration::from_secs(1)).await;

            // After init, stay in a loop to handle CRTP packets from crtp_rx_in
            loop {
                tokio::select! {
                    _ = close_rx_init.recv_async() => break,
                    packet_res = crtp_rx_in_init.recv_async() => {
                        if let Ok(packet) = packet_res {
                            let crtp_data: Vec<u8> = packet.into();
                            let length = (crtp_data.len() + 2) as u16;
                            let route_byte = (0 << 7) | (0 << 6) | (CPX_T_WIFI_HOST << 3) | CPX_T_STM32;
                            let func_byte = (0 << 6) | CPX_F_CRTP;
                            let mut buf = Vec::with_capacity(4 + crtp_data.len());
                            let _ = <Vec<u8> as WriteBytesExt>::write_u16::<LittleEndian>(&mut buf, length);
                            buf.push(route_byte);
                            buf.push(func_byte);
                            buf.extend_from_slice(&crtp_data);
                            if let Err(e) = writer_init.write_all(&buf).await {
                                tracing::error!("WiFi Link write error: {}", e);
                                break;
                            }
                        } else {
                            break;
                        }
                    }
                }
            }
            *status_clone_init.lock().await = ConnectionStatus::Disconnected("TCP stream closed".to_string());
        });

        // Downlink task (Reader)
        let image_tx_clone = image_tx.clone();
        let status_clone_reader = status.clone();
        tokio::spawn(async move {
            let mut header_buf = [0u8; 4];
            loop {
                if let Err(e) = reader.read_exact(&mut header_buf).await {
                    tracing::info!("WiFi Link read error: {}", e);
                    break;
                }

                let length = u16::from_le_bytes([header_buf[0], header_buf[1]]);
                let route_byte = header_buf[2];
                let func_byte = header_buf[3];

                let dst = route_byte & 0x07;
                let function = func_byte & 0x3F;

                let data_len = (length as usize).saturating_sub(2);
                let mut data = vec![0u8; data_len];
                if let Err(e) = reader.read_exact(&mut data).await {
                    tracing::info!("WiFi Link read data error: {}", e);
                    break;
                }

                if function == CPX_F_CRTP && dst == CPX_T_WIFI_HOST {
                    let packet = Packet::from(data);
                    let _ = crtp_tx_in.send_async(packet).await;
                } else if function == CPX_F_APP {
                    let _ = image_tx_clone.send_async(data).await;
                }
            }
            *status_clone_reader.lock().await = ConnectionStatus::Disconnected("TCP stream closed".to_string());
        });

        Ok(Self {
            status,
            crtp_rx: crtp_rx_out,
            crtp_tx: crtp_tx_out,
            close_tx,
        })
    }
}

#[async_trait]
impl ConnectionTrait for WifiLink {
    async fn wait_close(&self) -> String {
        loop {
            let status = self.status.lock().await;
            if let ConnectionStatus::Disconnected(ref reason) = *status {
                return reason.clone();
            }
            drop(status);
            tokio::time::sleep(tokio::time::Duration::from_millis(100)).await;
        }
    }

    async fn close(&self) {
        let _ = self.close_tx.send_async(()).await;
    }

    async fn status(&self) -> ConnectionStatus {
        self.status.lock().await.clone()
    }

    async fn wait_disconnect(&self) {
        self.wait_close().await;
    }

    async fn send_packet(&self, packet: Packet) -> LinkResult<()> {
        self.crtp_tx.send_async(packet).await.map_err(|_| LinkError::InvalidUri) // FIXME error type
    }

    async fn recv_packet(&self) -> LinkResult<Packet> {
        self.crtp_rx.recv_async().await.map_err(|_| LinkError::InvalidUri) // FIXME error type
    }

    async fn link_statistics(&self) -> Option<RadioLinkStatistics> {
        None
    }
}
