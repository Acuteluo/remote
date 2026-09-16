#!/bin/bash
# 探测内置 DMIC(数字麦克风)到底能不能采到声音。
#
# 背景: 这台机器上 UCM 没能初始化 DMIC(声卡 components 属性缺失), 麦克风采集
# 一打开就挂起。这里绕过 PipeWire 直接问 ALSA 硬件, 试 4 通道和 2 通道 ——
# UCM 里 DMIC 配的是 CapturePCM "hw:0,6" + CaptureChannels 4。
#
# **关键**: 用 trap 保证无论中间发生什么, 退出前一定把音频服务拉回来,
# 不能把已经修好的"电脑声音转发"弄坏。
set +e

restore() {
    echo "=== 恢复音频服务 ==="
    systemctl --user start pipewire pipewire-pulse wireplumber 2>/dev/null
    sleep 3
    systemctl --user is-active pipewire pipewire-pulse wireplumber 2>&1 | tr '\n' ' '
    echo
}
trap restore EXIT

echo "=== 0. 备份当前 ALSA 状态 ==="
echo 1 | sudo -S -p "" alsactl store 2>&1 | tail -1

echo "=== 1. 停音频服务(释放 hw:0,6) ==="
systemctl --user stop wireplumber pipewire-pulse pipewire 2>&1
sleep 1

# 注意格式: DMIC 只支持 S32_LE(上面 arecord 报的 Available formats),
# 用 S16_LE 会直接打不开 —— 这就是之前一直"采不到"的直接原因之一。
for spec in "4 S32_LE" "2 S32_LE" "2 S16_LE"; do
    set -- $spec
    ch="$1"; fmt="$2"
    echo "=== 2. 直采 hw:0,6 (${ch} 通道, ${fmt}, 3 秒) ==="
    f="/tmp/dmic_${ch}_${fmt}.wav"
    rm -f "$f"
    timeout 10 arecord -D hw:0,6 -f "$fmt" -r 48000 -c "$ch" -d 3 "$f" 2>&1 | tail -2
    if [ -s "$f" ]; then
        ffmpeg -hide_banner -i "$f" -af volumedetect -f null - 2>&1 \
            | grep -E "max_volume|mean_volume"
    else
        echo "  (没录到数据)"
    fi
done

echo "=== 3. 顺便看看 mixer 里 DMIC 相关的开关 ==="
amixer -c0 scontrols 2>&1 | grep -iE "dmic|mic" | head -6
