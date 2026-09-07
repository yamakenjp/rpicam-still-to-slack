# rpicam-still-to-slack

Raspberry Pi Zero 2 W と Raspberry Pi Camera Module 3 で屋外を撮影し、Slack に投稿するためのスクリプトです。

このリポジトリは Raspberry Pi OS Trixie への移行後の環境に固定します。Bookworm / Bullseye 互換は考慮しません。

常駐プロセスではなく、cron または systemd timer から 15 分に 1 回実行する前提です。

## 方針

- 対象 OS は Raspberry Pi OS Trixie に固定する
- 撮影は `rpicam-still` に任せる
- 制御と Slack 投稿は Python 3 で行う
- HDR は設定可能とし、初期値は `auto` にする
- 投稿前に事前撮影を行い、露光時間と総ゲインから本撮影のプロファイルを決める
- 固定シャッターによる白飛びを避けるため、自動シャッターは初期値で夜間だけに適用する
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
sudo apt install -y python3-venv python3-pip libimage-exiftool-perl

git clone https://github.com/yamakenjp/rpicam-still-to-slack.git
cd rpicam-still-to-slack

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

install -m 600 .slack_option.sample .slack_option
vim .slack_option

install -m 600 .camera_option.sample .camera_option
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

`.slack_option` には Bot Token が入るため、パーミッションは `600` にしてください。起動時にもグループまたは他ユーザーから読める状態を検出して警告します。

## カメラ設定

`.camera_option` を作成します。

主な設定です。

```sh
OUTPUT_PATH=/tmp/image.jpg
FINAL_METADATA_PATH=/tmp/rpicam-still-to-slack-final.json
EMBED_EXIF_METADATA=1
EXIFTOOL=exiftool
EXIF_METADATA_REQUIRED=0
```

`FINAL_METADATA_PATH` は本撮影の `rpicam-still --metadata` 出力先です。省略時は `/tmp/rpicam-still-to-slack-final.json` を使います。

`EMBED_EXIF_METADATA=1` の場合、本撮影後かつ Slack 投稿前に `embed_metadata.py` を実行し、本撮影メタデータを JPEG の EXIF に書き込みます。無効化する場合は `EMBED_EXIF_METADATA=0` を指定します。

`EXIFTOOL` は `embed_metadata.py` が使う ExifTool コマンドです。省略時は `exiftool` を使います。

`EXIF_METADATA_REQUIRED=1` の場合、EXIF 書き込み失敗を致命的エラーとして扱い、Slack 投稿に進みません。省略時や `0` では警告にして撮影処理を継続します。

## 露出制御

事前撮影の `ExposureTime` と、`AnalogueGain * DigitalGain` で求めた総ゲインから `day` / `twilight` / `night` を判定します。`FrameDuration` は露光時間ではないため判定には使いません。

初期値では自動シャッターを `night` にだけ適用します。昼間と薄暮では `--shutter` を指定せず、`rpicam-still` の AE に任せます。

```sh
AUTO_SHUTTER=1
AUTO_SHUTTER_PROFILES=night
AUTO_SHUTTER_TARGET_GAIN=2.0
AUTO_SHUTTER_MIN_US=1000
AUTO_SHUTTER_MAX_US=12000000
AUTO_SHUTTER_STEP_US=1000
```

夜間の推定値は、おおむね次の式で計算します。

```text
本撮影シャッター = 事前撮影の露光時間 × max(総ゲイン ÷ AUTO_SHUTTER_TARGET_GAIN, 1)
```

現在より短いシャッターにはせず、余分なゲインだけを長い露光へ振り替えます。`AUTO_SHUTTER_TARGET_GAIN` は `1.0` 以上、推定値は `AUTO_SHUTTER_MIN_US` から `AUTO_SHUTTER_MAX_US` の範囲に制限されます。時間の値はマイクロ秒です。

プロファイルの判定しきい値は次の設定で調整できます。

```sh
EXPOSURE_DAY_MAX_US=2000
GAIN_DAY_MAX=2.0
EXPOSURE_NIGHT_MIN_US=10000
GAIN_NIGHT_MIN=4.0
```

`DAY_SHUTTER_US`、`TWILIGHT_SHUTTER_US`、`NIGHT_SHUTTER_US` に正の値を指定すると、そのプロファイルの自動推定より優先して固定シャッターとして使います。初期値はすべて `0` です。自動推定を使うプロファイルでは `0` にしてください。例えば、夜景を4秒露光へ固定する場合は `NIGHT_SHUTTER_US=4000000` とします。

### 旧設定からの移行

Lux 基準だった以下の設定は廃止され、指定されていても無視されます。

- `AUTO_SHUTTER_REFERENCE_US`
- `AUTO_SHUTTER_REFERENCE_LUX`
- `AUTO_SHUTTER_DAY_MAX_US`
- `AUTO_SHUTTER_TWILIGHT_MAX_US`

既存の `.camera_option` から上記を削除し、`AUTO_SHUTTER_PROFILES=night` と `AUTO_SHUTTER_TARGET_GAIN=2.0` を追加してください。`AUTO_SHUTTER_MIN_US` は `50000` から `1000` へ変更します。従来どおり夜景を4秒露光にする場合は `NIGHT_SHUTTER_US=4000000` を残し、適応シャッターへ切り替える場合だけ `NIGHT_SHUTTER_US=0` に変更します。

## フォーカス調整

Camera Module 3 のピントは `lens-position` で固定できます。単位はジオプトリで、`0.0` が無限遠、`2.0` が約 0.5 m です。このスクリプトでは、`.camera_option` に `LENS_POSITION` がある場合、その値を優先して通常撮影します。

```sh
LENS_POSITION=0.0
```

`LENS_POSITION` が設定されている場合、通常撮影ではフォーカス追い込み用の複数事前撮影を行いません。事前撮影と本撮影の両方で `--autofocus-mode manual --lens-position <LENS_POSITION>` を使います。

通常撮影のログに以下が出れば、固定フォーカスが使われています。

```text
using configured LENS_POSITION=0.0; focus pre-captures are skipped
```

本撮影コマンドには以下が含まれます。

```text
--autofocus-mode manual --lens-position 0.000000
```

値は撮影対象との距離や個体差に合わせてキャリブレーションしてください。上記の `0.0` は遠景向けの説明例であり、推奨値を固定するものではありません。

`LENS_POSITION` が未設定の場合、通常撮影では事前撮影を複数回行い、各回の `FocusFoM` と `LensPosition` を比較します。その中で `FocusFoM` が最大の `LensPosition` を本撮影に使います。

事前撮影回数は以下で指定できます。

```sh
FOCUS_PRECAPTURE_COUNT=3
```

### フォーカスキャリブレーションモード

`--focus-calibration` を使うと、複数の `lens-position` 値で撮影し、`FocusFoM` が最も高い値を推奨値として出力します。Slack には投稿しません。

```sh
./capture_to_slack.py --focus-calibration --debug --no-upload
```

標準では `0.0` から `8.0` まで `0.5` 刻みでスキャンします。

```sh
FOCUS_SCAN_START=0.0
FOCUS_SCAN_END=8.0
FOCUS_SCAN_STEP=0.5
```

範囲を細かく指定したい場合は `FOCUS_SCAN_VALUES` を使います。

```sh
FOCUS_SCAN_VALUES=0.0,0.1,0.2,0.3,0.4,0.5
```

出力例です。

```text
recommended LENS_POSITION=0.2 focus_fom=3816.0
LENS_POSITION=0.2
FOCUS_FOM=3816.0
FOCUS_CALIBRATION_REPORT=/tmp/rpicam-still-to-slack-focus/results.csv
```

推奨値が決まったら、`.camera_option` に `LENS_POSITION=<value>` を設定します。調整に使った `FOCUS_SCAN_VALUES` は通常運用では削除して構いません。

```sh
LENS_POSITION=0.2
```

`FOCUS_SCAN_STEP` は正の値、`FOCUS_SCAN_END` は `FOCUS_SCAN_START` 以上である必要があります。不正な範囲は撮影前にエラーになります。`--focus-calibration --dry-run` はコマンドだけを表示し、実測値がないため推奨 `LENS_POSITION` を出力しません。

キャリブレーション結果の画像と JSON は以下に保存されます。

```sh
/tmp/rpicam-still-to-slack-focus/
```

CSV レポートは以下です。

```sh
/tmp/rpicam-still-to-slack-focus/results.csv
```

最終判断では、キャリブレーション時の `FocusFoM` だけでなく、通常撮影後の本撮影メタデータの `FocusFoM` と実画像の見た目も確認してください。夜間では露光時間やノイズにより `FocusFoM` が揺らぎます。

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
- 本撮影の JSON メタデータをログに出す
- 事前撮影画像、事前撮影メタデータ、本撮影メタデータを削除せず保持する

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

## タイムアウトと設定検証

`rpicam-still`、ExifTool、Slack 通信には個別のタイムアウトがあります。

```sh
COMMAND_TIMEOUT_S=45
EXIFTOOL_TIMEOUT_S=30
SLACK_TIMEOUT_S=30
```

`COMMAND_TIMEOUT_S` はカメラコマンドの最低タイムアウトです。指定シャッターと `rpicam-still --timeout` の合計が長い場合は、撮影完了に必要な時間を自動的に加算します。数値の範囲、露出・ゲインしきい値の順序、フォーカス走査間隔などは撮影開始前に検証されます。

## systemd timer

リポジトリを実行ユーザーのホーム直下の `rpicam-still-to-slack` に配置した前提の unit を同梱しています。初期ユーザーは `pi` です。

```sh
sudo cp systemd/rpicam-still-to-slack.service /etc/systemd/system/
sudo cp systemd/rpicam-still-to-slack.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rpicam-still-to-slack.timer
```

同梱の service はプロセス全体を `120s` で打ち切り、作成ファイルの既定パーミッションを `077` にします。実行ユーザーが異なる場合は `User` と `Group` を変更してください。配置先が異なる場合は `WorkingDirectory` と `ExecStart` も変更します。

状態確認です。

```sh
systemctl status rpicam-still-to-slack.timer
journalctl -u rpicam-still-to-slack.service -n 100 --no-pager
```

## 撮影フロー

1. ロックファイルを取得する
2. `LENS_POSITION` の有無を確認する
3. `LENS_POSITION` がある場合は固定フォーカスで事前撮影する
4. `LENS_POSITION` がない場合は複数回の事前撮影で `FocusFoM` が最大の `LensPosition` を選ぶ
5. `rpicam-still` の JSON メタデータを読む
6. 露光時間と総ゲインから `day` / `twilight` / `night` のプロファイルを決める
7. 対象プロファイルが `night` の場合だけ、初期値では適応シャッター値を計算する
8. HDR 設定を維持したまま本撮影を行う
9. 本撮影の JSON メタデータを読む
10. 必要に応じて本撮影メタデータを JPEG の EXIF に書き込む
11. Slack に投稿する
12. 複数回のフォーカス事前撮影を含む一時ファイルを整理する

## テスト

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
```

## 注意

Camera Module 3 の HDR はセンサーとドライバ側の制約を受けます。高解像度よりも HDR を優先するため、初期値では投稿画像サイズを 2304x1296 にしています。

夜間の明るさが足りない場合、標準 Camera Module 3 では長秒露光頼みになります。真っ暗な屋外を撮る場合は NoIR 版と赤外線照明の利用を検討してください。
