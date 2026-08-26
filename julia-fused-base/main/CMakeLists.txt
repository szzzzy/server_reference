# 将服务器根证书嵌入最终固件，供 HTTPS OTA 客户端进行证书链校验。

idf_build_get_property(project_dir PROJECT_DIR)

set(embed_files
    ${project_dir}/server_certs/ca_cert.pem
)

if(CONFIG_COMM_DEVICE_AUTH_CERTIFICATE)
    list(APPEND embed_files
        ${project_dir}/server_certs/device_client_cert.pem
        ${project_dir}/server_certs/device_client_key.pem
    )
endif()

set(srcs
    # 应用入口、网络生命周期与通信层。
    "native_ota_example.c"
    "network_lifecycle.c"
    "mqtt_comm.c"

    # 语音服务：纯传输层 + 业务协议 + URI 映射 + 演示。
    "wss_transport.c"
    "voice_service.c"
    "voice_uri.c"
    "voice_push_demo.c"
    "sd_card.c"
    "tca9554.c"

    # 本地唤醒词（WakeNet "你好小智"）：检测到后自动开麦推流。
    "wake_detector.c"

    # 固件 OTA 模块。
    "ota_boot_health.c"
    "ota_control_plane.c"
    "ota_report.c"
    "ota_state_store.c"
    "ota_stability.c"

    # 公共 HTTPS 数据面下载器（固件 OTA 与音频素材共用）。
    "http_downloader.c"

    # 音频素材下载模块。
    "audio_control_plane.c"
    "audio_engine.c"
    "audio_service.c"
)
set(priv_requires
    esp_http_client
    app_update
    bootloader_support
    esp_hw_support
    spi_flash
    efuse
    heap
    nvs_flash
    esp_driver_gpio
    esp_netif
    esp_wifi
    esp_timer
    json
    mbedtls
    mqtt
    esp-tls
    lwip
    fatfs
    sdmmc
    esp_driver_sdmmc
    esp_driver_i2c
    esp_driver_spi
    esp_driver_i2s
    julia_board_audio
    espressif__esp-sr
)

idf_component_register(
    SRCS ${srcs}
    INCLUDE_DIRS "."
    PRIV_REQUIRES ${priv_requires}
    EMBED_TXTFILES ${embed_files}
)
