# rpicam-still-to-slack

Raspberry Pi Zero 2 W と Raspberry Pi Camera Module 3 で屋外を撮影し、Slack に投稿するためのスクリプトです。

このリポジトリは Raspberry Pi OS Trixie への移行後の環境に固定します。Bookworm / Bullseye 互換は考慮しません。

常駐プロセスではなく、cron または systemd timer から 15 分に 1 回実行する前提です。

## 方針

- 対象 OS は Raspberry Pi OS Trixie に固定する
- 撮影は `rpicam-still` に任せる
- 制御と Slack 投稿は Python 3 で行う
- HDR は常時有効化する
- 投稿前に事前撮影を行い、露出・ゲインなどのメタデータを見て本撮影のプロファイルを決める
- 日の出・日の入り API には依存しない
- Slack へのファイル投稿は Slack SDK の `files_upload_v2` を使う

## 必要なもの

- Raspberry Pi Zero 2 W
- Raspberry Pi Camera Module 3
- Raspberry Pi Zero 用カメラケーブル
- Raspberry Pi OS Trixie Lite
- `rpicam-still` が使える環境
- Slack Bot Token
- 投稿先チャンネル ID

## OS 前提

このスクリプトは Raspberry Pi OS Trixie Lite で動かす前提です。

Bookworm からのインプレースアップグレードではなく、Trixie Lite の新規インストール後にセットアップする方針にします。

Trixie 化後、まず以下を確認します。

```sh
cat /etc/os-release
python3 --version
rpicam-still --version
```

`/etc/os-release` で `VERSION_CODENAME=trixie` を確認してください。

## セットアップ

```sh
sudo apt update
sudo apt install -y python3-venv python3-pip

git clone https://github.com/yamakenjp/rpicam-still-to-slack.git
cd rpicam-still-to-slack

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

cp .slack_option.sample .slack_option
vim .slack_option

cp .camera_option.sample .camera_option
vim .camera_option
```

Raspberry Pi OS には `rpicam-apps` が含まれるため、通常は `rpicam-still` を追加ビルドしません。`rpicam-still --version` が通らない場合だけ OS / camera stack 側を確認します。

## Slack 設定

`.slack_option` を作成します。

```sh
SLACK_TOKEN=replace-with-your-slack-bot-token
CHANNEL=C0123456789
```

以下の名前も読めます。

```sh
SLACK_BOT_TOKEN=replace-with-your-slack-bot-token
SLACK_CHANNEL_ID=C0123456789
```

Bot には少なくとも投稿先チャンネルへの参加と、ファイルアップロードに必要な権限が必要です。

## 手動実行

まず dry-run で撮影コマンドだけ確認します。

```sh
. .venv/bin/activate
./capture_to_slack.py --dry-run
```

実際に撮影して Slack に投稿します。

```sh
./capture_to_slack.py
```

標準では投稿画像は `/tmp/image.jpg` に作成されます。

## デバッグ

コマンドラインでデバッグモードを有効にできます。

```sh
./capture_to_slack.py --debug
```

`.camera_option` で常時有効にする場合は以下を指定します。

```sh
DEBUG=1
```

デバッグモードでは以下を行います。

- ログレベルを DEBUG にする
- 読み込んだ設定をログに出す
- Slack token はマスクして出す
- 事前撮影の JSON メタデータをログに出す
- 事前撮影画像とメタデータファイルを削除せず保持する

撮影は行うが Slack には投稿しない場合は、以下を使います。

```sh
./capture_to_slack.py --no-upload
```

`.camera_option` で指定する場合は以下です。

```sh
DEBUG_NO_UPLOAD=1
```

デバッグ撮影だけ行う場合は以下が便利です。

```sh
./capture_to_slack.py --debug --no-upload
```

## systemd timer

リポジトリを `/home/pi/rpicam-still-to-slack` に配置した前提の unit を同梱しています。

```sh
sudo cp systemd/rpicam-still-to-slack.service /etc/systemd/system/
sudo cp systemd/rpicam-still-to-slack.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rpicam-still-to-slack.timer
```

状態確認です。

```sh
systemctl status rpicam-still-to-slack.timer
journalctl -u rpicam-still-to-slack.service -n 100 --no-pager
```

## 撮影フロー

1. ロックファイルを取得する
2. 事前撮影を行う
3. `rpicam-still` の JSON メタデータを読む
4. `day` / `twilight` / `night` のプロファイルを決める
5. HDR 有効のまま本撮影を行う
6. Slack に投稿する
7. 一時ファイルを整理する

## 注意

Camera Module 3 の HDR はセンサーとドライバ側の制約を受けます。高解像度よりも HDR を優先するため、初期値では投稿画像サイズを 2304x1296 にしています。

夜間の明るさが足りない場合、標準 Camera Module 3 では長秒露光頼みになります。真っ暗な屋外を撮る場合は NoIR 版と赤外線照明の利用を検討してください。
