# gen_compile_commands.ps1 —— julia-fused-base IntelliSense 编译数据库生成器
# 从 fused (IDF 5.5.4) 的编译条目提取「GCC 前缀 + 全部 -I 路径」，替换为 base 路径，
# 然后为 base 每个 .c 生成精确条目（单源文件 + -o + -c）。
$ErrorActionPreference = 'Stop'
$root = 'D:\Espressif\projects\julia-fused-base'
$fusedCJ = 'D:\Espressif\projects\julia-esp32s3-ai-terminal-fused\build\compile_commands.json'
$out = "$root\build\compile_commands.json"

$json = Get-Content $fusedCJ -Raw | ConvertFrom-Json

# 模板（julia_voice.c 条目：含全部 IDF + esp-sr include）
$tmpl = $json | Where-Object { $_.file -match 'julia_voice\.c' } | Select-Object -First 1
$tokens = $tmpl.command -split ' '

# 从模板提取：gcc 前缀 + 所有 -I/-D 等编译选项（不含 -o/-c/源文件）
$prefix = @()
$i = 0
while ($i -lt $tokens.Count) {
    $t = $tokens[$i]
    if ($t -eq '-o' -or $t -eq '-c') { $i += 2; continue }
    if ($t -match '\.c$') { $i += 1; continue }
    $prefix += $t
    $i += 1
}
# 路径替换：fused -> base
$prefixStr = ($prefix -join ' ')
$prefixStr = $prefixStr.Replace('D:/Espressif/projects/julia-esp32s3-ai-terminal-fused/waveshare_demo/ESP-IDF/ESP32-S3-LCD-1.85-Test/components/espressif__esp-sr', 'D:/Espressif/projects/julia-fused-base/components/espressif__esp-sr')
$prefixStr = $prefixStr.Replace('D:/Espressif/projects/julia-esp32s3-ai-terminal-fused/waveshare_demo/ESP-IDF/ESP32-S3-LCD-1.85-Test/components/espressif__esp-dsp', 'D:/Espressif/projects/julia-fused-base/components/espressif__esp-dsp')
$prefixStr = $prefixStr.Replace('D:/Espressif/projects/julia-esp32s3-ai-terminal-fused/main', 'D:/Espressif/projects/julia-fused-base/main')
$prefixStr = $prefixStr.Replace('D:/Espressif/projects/julia-esp32s3-ai-terminal-fused', 'D:/Espressif/projects/julia-fused-base')
# 过滤 fused 专属 include（lvgl / julia_wireless_voice / julia_ota / 其他 waveshare 组件）
$toks2 = $prefixStr -split ' '
$kept = @()
foreach ($t in $toks2) {
    if ($t -match '^\-I' -and ($t -match 'lvgl|julia_wireless_voice|julia_ota|julia_board_audio_minimal|waveshare_demo')) { continue }
    $kept += $t
}
$prefixBase = ($kept -join ' ')

# base 特有 include（组件 include + esp-tls/efuse/netif/sdmmc + 板级音频）
$extraStr = '-ID:/Espressif/projects/julia-fused-base/components/julia_board_audio/include -ID:/Espressif/v5.5.4/esp-idf/components/esp-tls -ID:/Espressif/v5.5.4/esp-idf/components/efuse/include -ID:/Espressif/v5.5.4/esp-idf/components/efuse/esp32s3/include -ID:/Espressif/v5.5.4/esp-idf/components/esp_netif/include -ID:/Espressif/v5.5.4/esp-idf/components/esp_driver_sdmmc/include -ID:/Espressif/v5.5.4/esp-idf/components/sdmmc/include -ID:/Espressif/v5.5.4/esp-idf/components/esp_driver_gpio/include'

# base 源文件
$baseSrcs = @()
Get-ChildItem "$root\main" -Filter '*.c' -File | ForEach-Object { $baseSrcs += $_.FullName }
Get-ChildItem "$root\components\julia_board_audio" -Filter '*.c' -File | ForEach-Object { $baseSrcs += $_.FullName }

$entries = @()
foreach ($src in $baseSrcs) {
    $rel = $src.Substring($root.Length + 1).Replace('\', '/')
    $objPath = 'D:/Espressif/projects/julia-fused-base/build/obj/' + ($rel -replace '/', '_') + '.o'
    $cmd = "$prefixBase $extraStr -o $objPath -c `"$src`""
    $entries += [pscustomobject]@{ directory = $root; file = $src; command = $cmd }
}
$entries | ConvertTo-Json -Depth 3 | Set-Content $out -Encoding ascii
Write-Output "compile_commands.json ok ($($entries.Count) entries)"
