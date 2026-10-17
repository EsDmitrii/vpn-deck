import {
  ButtonItem,
  DialogBody,
  DialogButton,
  DialogButtonPrimary,
  DialogFooter,
  DialogHeader,
  DropdownItem,
  Field,
  ModalRoot,
  PanelSection,
  PanelSectionRow,
  showModal,
  staticClasses,
  TextField,
  ToggleField,
} from "@decky/ui";
import {
  callable,
  definePlugin,
  FileSelectionType,
  openFilePicker,
  toaster,
} from "@decky/api";
import { useState, useEffect, useCallback } from "react";
import { FaNetworkWired } from "react-icons/fa";

interface VPNError {
  timestamp: number;
  operation: string;
  error_type: string;
  message: string;
  details: Record<string, any>;
}

// Mirrors py_modules/vpn_deck/models.py (ConfigKind, ConnectionStatus, ...).
type ConfigKind = "awg" | "vless" | "singbox" | "subscription";

interface ConfigInfo {
  name: string;
  kind: ConfigKind;
  label: string; // e.g. "AWG", "VLESS REALITY"
  interface: string;
  active: boolean;
  managed_by: string;
  server: string | null; // subscription: picked server
}

interface ExitInfo {
  ip: string;
  country: string | null;
  checked_at: number;
}

interface ConnectionStatus {
  name: string;
  kind: ConfigKind;
  interface: string;
  addresses: string[];
  latest_handshake: number | null; // unix time, AWG only
  rx_bytes_per_sec: number | null;
  tx_bytes_per_sec: number | null;
  exit: ExitInfo | null;
}

interface RpcResult {
  success: boolean;
  error: string | null;
}

interface ImportInspection extends RpcResult {
  kind?: ConfigKind;
  label?: string;
  mtu?: number | null;
  mtu_warning?: boolean;
  warnings?: string[];
  servers?: string[]; // subscription only
}

interface ImportResult extends RpcResult {
  kind?: ConfigKind;
  warnings?: string[];
}

interface PluginSettings {
  restore_on_boot: boolean;
  vless_dns_server: string;
  vless_ipv4_only: boolean;
}

interface VPNOpResult {
  success: boolean;
  error: string | null;
  interface: string;
}

const listConfigsWithStatus = callable<[], ConfigInfo[]>(
  "list_configs_with_status",
);
const vpnStartConfig = callable<[{ config_name: string }], VPNOpResult>(
  "vpn_start_config",
);
const vpnStopConfig = callable<[{ config_name: string }], VPNOpResult>(
  "vpn_stop_config",
);
const getConnectionStatus = callable<[], ConnectionStatus | null>("get_connection_status");
const inspectImport = callable<[string], ImportInspection>("inspect_import");
const inspectText = callable<[string], ImportInspection>("inspect_text");
const importText = callable<[string, string, number | null, string | null], ImportResult>("import_text");
const importVpnConfig = callable<[string, string, number | null, string | null], ImportResult>(
  "import_vpn_config",
);
const getSubscriptionServers = callable<[string], RpcResult & { servers?: string[] }>(
  "get_subscription_servers",
);
const setSubscriptionServer = callable<[string, string], RpcResult>("set_subscription_server");
const getSettings = callable<[], PluginSettings>("get_settings");
const setSetting = callable<[string, PluginSettings[keyof PluginSettings]], RpcResult>("set_setting");
const getErrors = callable<[], VPNError[]>("get_errors");
const clearErrors = callable<[], boolean>("clear_errors");

interface DeleteConfigResult {
  success: boolean;
  config_name: string | null;
  error?: string | null;
}
const deleteVpnConfig = callable<[{ name: string }] | [string], DeleteConfigResult>(
  "delete_vpn_config"
);

interface SymlinkRepairItem {
  name: string;
  interface: string;
  ok: boolean;
  action: "none" | "created" | "replaced" | "error";
  error: string | null;
}
interface SymlinkRepairResult {
  total: number;
  repaired: number;
  results: SymlinkRepairItem[];
}
const repairSymlinks = callable<[], SymlinkRepairResult>("repair_symlinks");

interface DiagnosticsProbe {
  name: string;
  kind: "ping" | "http" | "trace";
  target: string;
  ok: boolean;
  detail: string;
  latency_ms: number | null;
}
const diagnoseConnectivity = callable<[], DiagnosticsProbe[]>("diagnose_connectivity");

// Валидация имени конфига: как в awg-quick и config_manager (интерфейс = vd-<name>, макс. 15 символов)
const CONFIG_NAME_MAX_LEN = 12; // 3 (префикс "vd-") + 12 = 15
const CONFIG_NAME_REGEX = /^[a-zA-Z0-9_=+.-]+$/;

const STACK = { display: "flex", flexDirection: "column", gap: "12px" } as const;
const TEXT = { fontSize: "13px" } as const;
const HINT = { marginTop: "6px", fontSize: "12px", color: "#8b929a" } as const;
const COLOR_ERROR = "#f87171";
const COLOR_WARNING = "#fbbf24";

/**
 * Modal footer with buttons in a row, primary action first (as in Steam's own
 * dialogs). DialogFooter alone stacks full-width buttons with no gap.
 */
function ModalActions({
  primary,
  onPrimary,
  primaryDisabled,
  onCancel,
  cancelLabel = "Отмена",
}: {
  primary?: string;
  onPrimary?: () => void;
  primaryDisabled?: boolean;
  onCancel?: () => void;
  cancelLabel?: string;
}) {
  return (
    <DialogFooter>
      <div style={{ display: "flex", gap: "8px" }}>
        {primary && (
          <DialogButtonPrimary style={{ flex: 1 }} onClick={onPrimary} disabled={primaryDisabled}>
            {primary}
          </DialogButtonPrimary>
        )}
        <DialogButton style={{ flex: 1 }} onClick={onCancel}>
          {cancelLabel}
        </DialogButton>
      </div>
    </DialogFooter>
  );
}

const RECOMMENDED_AWG_MTU = 1280; // keep in sync with defaults.RECOMMENDED_AWG_MTU
const IMPORT_EXTENSIONS = ["conf", "txt", "vpn", "json"];

function formatTimestamp(timestamp: number): string {
  return new Date(timestamp * 1000).toLocaleString();
}

function formatSpeed(bytesPerSec: number | null): string {
  if (bytesPerSec === null) return "—";
  const units = ["Б/с", "КБ/с", "МБ/с", "ГБ/с"];
  let value = bytesPerSec;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function formatAgo(unixTime: number | null): string {
  if (unixTime === null) return "ещё не было";
  const sec = Math.max(0, Math.round(Date.now() / 1000 - unixTime));
  if (sec < 60) return `${sec} с назад`;
  if (sec < 3600) return `${Math.floor(sec / 60)} мин назад`;
  return `${Math.floor(sec / 3600)} ч назад`;
}

/** "NL" -> 🇳🇱 */
function countryFlag(code: string | null): string {
  if (!code || !/^[A-Z]{2}$/.test(code)) return "";
  return String.fromCodePoint(...[...code].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65));
}

/** Config name suggested from a file name: extension dropped, cut to the limit. */
function nameFromFile(path: string): string {
  const base = path.split("/").pop() ?? "";
  return base.replace(/\.(conf|txt|vpn|json)$/i, "").slice(0, CONFIG_NAME_MAX_LEN);
}

const DEFAULT_NAMES: Record<ConfigKind, string> = {
  awg: "awg",
  vless: "vless",
  singbox: "singbox",
  subscription: "sub",
};

/** `base`, or `base2`, `base3`… if taken. */
function uniqueName(base: string, taken: string[]): string {
  if (!taken.includes(base)) return base;
  for (let i = 2; ; i++) {
    const candidate = `${base.slice(0, CONFIG_NAME_MAX_LEN - String(i).length)}${i}`;
    if (!taken.includes(candidate)) return candidate;
  }
}

/** Where the config comes from: text in the field, or a picked file. */
type ImportSource = { kind: "text"; text: string } | { kind: "file"; path: string };

function validateConfigName(name: string): { valid: boolean; error?: string } {
  const trimmed = name.trim();
  if (!trimmed) {
    return { valid: false, error: "Введите имя конфига" };
  }
  if (trimmed.length > CONFIG_NAME_MAX_LEN) {
    return {
      valid: false,
      error: `Макс. ${CONFIG_NAME_MAX_LEN} символов (как в имени интерфейса)`,
    };
  }
  if (!CONFIG_NAME_REGEX.test(trimmed)) {
    return {
      valid: false,
      error: "Только буквы, цифры и символы _ = + . -",
    };
  }
  return { valid: true };
}

function ImportConfigModal({
  existingNames,
  closeModal,
  onSuccess,
}: {
  existingNames: string[];
  closeModal?: () => void;
  onSuccess: () => void;
}) {
  const [text, setText] = useState("");
  const [source, setSource] = useState<ImportSource | null>(null);
  const [name, setName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [inspection, setInspection] = useState<ImportInspection | null>(null);
  const [checking, setChecking] = useState(false);
  const [fixMtu, setFixMtu] = useState(true);
  const [server, setServer] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const applyInspection = (inspected: ImportInspection, suggested: string) => {
    setInspection(inspected);
    setServer(inspected.servers?.[0] ?? null);
    if (!nameTouched) setName(uniqueName(suggested, existingNames));
  };

  // Typed/pasted text is checked once the user stops typing.
  useEffect(() => {
    if (source?.kind === "file" || !text.trim()) {
      if (source?.kind !== "file") setInspection(null);
      return;
    }
    let cancelled = false; // a newer edit superseded this check
    const timer = setTimeout(async () => {
      setChecking(true);
      try {
        const inspected = await inspectText(text);
        if (cancelled) return;
        setSource({ kind: "text", text });
        applyInspection(inspected, DEFAULT_NAMES[inspected.kind ?? "awg"]);
      } finally {
        if (!cancelled) setChecking(false);
      }
    }, 600);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [text]);

  const handlePickFile = async () => {
    try {
      const res = await openFilePicker(
        FileSelectionType.FILE,
        "/home/deck/Downloads",
        true,
        true,
        undefined,
        IMPORT_EXTENSIONS,
      );
      setText("");
      setSource({ kind: "file", path: res.realpath });
      setChecking(true);
      applyInspection(await inspectImport(res.realpath), nameFromFile(res.realpath));
    } catch (e) {
      console.error(e);
    } finally {
      setChecking(false);
    }
  };

  const handleImport = async () => {
    if (!source) return;
    const validation = validateConfigName(name);
    if (!validation.valid) {
      toaster.toast({ title: "Ошибка", body: validation.error ?? "Неверное имя конфига" });
      return;
    }
    const trimmedName = name.trim();
    const mtu = inspection?.mtu_warning && fixMtu ? RECOMMENDED_AWG_MTU : null;
    setLoading(true);
    try {
      const result =
        source.kind === "text"
          ? await importText(trimmedName, source.text, mtu, server)
          : await importVpnConfig(trimmedName, source.path, mtu, server);
      if (!result.success) {
        toaster.toast({ title: "Ошибка импорта", body: result.error ?? "Неизвестная ошибка" });
        return;
      }
      toaster.toast({ title: "Конфиг импортирован", body: trimmedName });
      for (const warning of result.warnings ?? []) {
        toaster.toast({ title: "Внимание", body: warning });
      }
      onSuccess();
      closeModal?.();
    } catch (e) {
      toaster.toast({ title: "Ошибка", body: String(e) });
    } finally {
      setLoading(false);
    }
  };

  const sourceName = source?.kind === "file" ? source.path.split("/").pop() : "Ссылка";
  const canImport = !loading && !checking && !!inspection?.success;

  return (
    <ModalRoot onCancel={closeModal} closeModal={closeModal}>
      <DialogHeader>Импорт конфига</DialogHeader>
      <DialogBody>
        <div style={STACK}>
          <TextField
            label="Ссылка или URL подписки"
            description="vpn://, vless://, https://… подписки или JSON sing-box"
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              if (source?.kind === "file") setSource(null);
            }}
            disabled={loading}
          />
          <div>
            <DialogButton onClick={handlePickFile} disabled={loading}>
              {source?.kind === "file" ? "Выбрать другой файл" : "…или выбрать файл"}
            </DialogButton>
            <div style={HINT}>.conf, .txt со ссылкой, .json — например из «Загрузок»</div>
          </div>
          {checking && <div style={HINT}>Проверка…</div>}
          {inspection && !inspection.success && (
            <div style={{ ...TEXT, color: COLOR_ERROR }}>
              {sourceName}: {inspection.error}
            </div>
          )}
          {inspection?.success && (
            <>
              <div style={TEXT}>
                {sourceName} — {inspection.label}
              </div>
              <TextField
                label="Имя конфига"
                value={name}
                onChange={(e) => {
                  setName(e.target.value);
                  setNameTouched(true);
                }}
                description="до 12 символов: a-z, 0-9, _ = + . -"
              />
              {(inspection.servers?.length ?? 0) > 0 && (
                <ButtonItem
                  layout="below"
                  description="Потом можно сменить в списке конфигов"
                  onClick={() =>
                    showModal(
                      <ServerPickerModal
                        load={async () => inspection.servers ?? []}
                        current={server}
                        onPick={async (s) => {
                          setServer(s);
                          return true;
                        }}
                      />,
                    )
                  }
                >
                  Сервер: {server ?? "—"}
                </ButtonItem>
              )}
              {inspection.mtu_warning && (
                <ToggleField
                  label={`Установить MTU ${RECOMMENDED_AWG_MTU}`}
                  description={
                    inspection.mtu
                      ? `Сейчас MTU ${inspection.mtu}, для AWG это часто слишком много`
                      : "MTU не задан (будет ~1420), для AWG это часто слишком много"
                  }
                  checked={fixMtu}
                  onChange={setFixMtu}
                  bottomSeparator="none"
                />
              )}
              {(inspection.warnings ?? []).map((w) => (
                <div key={w} style={{ ...HINT, color: COLOR_WARNING }}>
                  ⚠ {w}
                </div>
              ))}
            </>
          )}
        </div>
      </DialogBody>
      <ModalActions
        primary={loading ? "Импорт…" : "Импортировать"}
        onPrimary={handleImport}
        primaryDisabled={!canImport}
        onCancel={closeModal}
      />
    </ModalRoot>
  );
}

/**
 * A config and the rows under it render as one group: only the group's last
 * element draws the separator line (Decky draws one under every field).
 */
type Separator = "none" | "standard";

function StatusRows({ status, separator }: { status: ConnectionStatus; separator: Separator }) {
  const exit = status.exit
    ? `${countryFlag(status.exit.country)} ${status.exit.ip}`.trim()
    : "проверяется…";
  const rows: [string, string][] = [["Выход в интернет", exit]];
  if (status.addresses.length > 0) rows.push(["Адрес в VPN", status.addresses[0]]);
  rows.push(["Скорость", `↓ ${formatSpeed(status.rx_bytes_per_sec)} ↑ ${formatSpeed(status.tx_bytes_per_sec)}`]);
  if (status.kind === "awg") rows.push(["Handshake", formatAgo(status.latest_handshake)]);
  return (
    <>
      {rows.map(([label, value], i) => (
        <Field
          key={label}
          label={label}
          padding="compact"
          bottomSeparator={i === rows.length - 1 ? separator : "none"}
        >
          {value}
        </Field>
      ))}
    </>
  );
}

function SettingsSection() {
  const [settings, setSettings] = useState<PluginSettings | null>(null);

  useEffect(() => {
    getSettings().then(setSettings).catch((e) => console.error("Failed to load settings:", e));
  }, []);

  const update = async <K extends keyof PluginSettings>(key: K, value: PluginSettings[K]) => {
    const res = await setSetting(key, value);
    if (!res.success) {
      toaster.toast({ title: "Ошибка", body: res.error ?? "Не удалось сохранить" });
      return;
    }
    setSettings((prev) => (prev ? { ...prev, [key]: value } : prev));
  };

  if (!settings) return null;
  return (
    <PanelSection title="Настройки">
      <PanelSectionRow>
        <ToggleField
          label="Подключать после перезагрузки"
          description="Включить конфиг, который был включён до выключения Deck"
          checked={settings.restore_on_boot}
          onChange={(v) => update("restore_on_boot", v)}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ToggleField
          label="VLESS: только IPv4"
          description={`DNS ${settings.vless_dns_server} через прокси. Выключите, если сервер умеет IPv6`}
          checked={settings.vless_ipv4_only}
          onChange={(v) => update("vless_ipv4_only", v)}
        />
      </PanelSectionRow>
    </PanelSection>
  );
}

function DeleteConfigModal({
  configs,
  closeModal,
  onSuccess,
}: {
  configs: ConfigInfo[];
  closeModal?: () => void;
  onSuccess: () => void;
}) {
  const [configName, setConfigName] = useState(configs[0]?.name ?? "");
  const [loading, setLoading] = useState(false);
  const active = configs.find((c) => c.name === configName)?.active;

  const handleDelete = async () => {
    try {
      setLoading(true);
      const result = await deleteVpnConfig({ name: configName });
      if (result.success) {
        toaster.toast({ title: "Конфиг удалён", body: configName });
        onSuccess();
        closeModal?.();
      } else {
        toaster.toast({ title: "Ошибка удаления", body: result.error ?? "Неизвестная ошибка" });
      }
    } catch (e) {
      toaster.toast({ title: "Ошибка", body: String(e) });
    } finally {
      setLoading(false);
    }
  };

  return (
    <ModalRoot onCancel={closeModal} closeModal={closeModal}>
      <DialogHeader>Удалить конфиг</DialogHeader>
      <DialogBody>
        <div style={STACK}>
          <DropdownItem
            label="Конфиг"
            rgOptions={configs.map((c) => ({ data: c.name, label: `${c.name} (${c.label})` }))}
            selectedOption={configName}
            onChange={(opt) => setConfigName(opt.data)}
            bottomSeparator="none"
          />
          {active && <div style={TEXT}>Этот конфиг сейчас включён, VPN отключится.</div>}
        </div>
      </DialogBody>
      <ModalActions
        primary={loading ? "Удаление…" : "Удалить"}
        onPrimary={handleDelete}
        primaryDisabled={loading || !configName}
        onCancel={closeModal}
      />
    </ModalRoot>
  );
}

/** Full-size list of subscription servers; picking one applies it and closes. */
function ServerPickerModal({
  load,
  current,
  onPick,
  closeModal,
}: {
  load: () => Promise<string[]>;
  current: string | null;
  onPick: (server: string) => Promise<boolean>;
  closeModal?: () => void;
}) {
  const [servers, setServers] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    load()
      .then(setServers)
      .catch((e) => setError(String(e)));
  }, []);

  const pick = async (server: string) => {
    if (server === current) {
      closeModal?.();
      return;
    }
    setBusy(true);
    try {
      if (await onPick(server)) closeModal?.();
    } finally {
      setBusy(false);
    }
  };

  return (
    <ModalRoot onCancel={closeModal} closeModal={closeModal}>
      <DialogHeader>Сервер подписки</DialogHeader>
      <DialogBody>
        {servers === null && !error && <div style={TEXT}>Загрузка списка серверов…</div>}
        {error && <div style={{ ...TEXT, color: COLOR_ERROR }}>{error}</div>}
        {servers && (
          <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
            {servers.map((s) => (
              <DialogButton
                key={s}
                disabled={busy}
                onClick={() => pick(s)}
                style={{ textAlign: "left", fontWeight: s === current ? "bold" : undefined }}
              >
                {s === current ? `✓ ${s}` : s}
              </DialogButton>
            ))}
          </div>
        )}
      </DialogBody>
      <ModalActions onCancel={closeModal} cancelLabel="Закрыть" />
    </ModalRoot>
  );
}

/** "Server: X" row under a subscription; opens ServerPickerModal. */
function SubscriptionServerButton({
  cfg,
  onChanged,
  separator,
}: {
  cfg: ConfigInfo;
  onChanged: () => void;
  separator: Separator;
}) {
  const load = async () => {
    const res = await getSubscriptionServers(cfg.name);
    if (!res.success || !res.servers) throw new Error(res.error ?? "Не удалось загрузить серверы");
    return res.servers;
  };
  const pick = async (server: string) => {
    const res = await setSubscriptionServer(cfg.name, server);
    toaster.toast(res.success ? { title: "Сервер сменён", body: server } : { title: "Ошибка", body: res.error ?? "" });
    onChanged();
    return res.success;
  };
  const open = () => showModal(<ServerPickerModal load={load} current={cfg.server} onPick={pick} />);
  // A whole-row link like Steam's settings entries, not a big button.
  return (
    <Field
      label="Сервер"
      description={cfg.server ?? "—"}
      bottomSeparator={separator}
      focusable
      highlightOnFocus
      onActivate={open}
      onClick={open}
    >
      <span style={{ fontSize: "20px", color: "#8b929a" }}>›</span>
    </Field>
  );
}

function Content() {
  const [configs, setConfigs] = useState<ConfigInfo[]>([]);
  const [status, setStatus] = useState<ConnectionStatus | null>(null);
  const [loadingMap, setLoadingMap] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<VPNError[]>([]);
  const [showErrors, setShowErrors] = useState<boolean>(false);
  const [probes, setProbes] = useState<DiagnosticsProbe[] | null>(null);
  const [probesLoading, setProbesLoading] = useState<boolean>(false);
  const [repairLoading, setRepairLoading] = useState<boolean>(false);

  const refreshConfigs = useCallback(async () => {
    try {
      const result = await listConfigsWithStatus();
      setConfigs(result);
      setStatus(result.some((c) => c.active) ? await getConnectionStatus() : null);
    } catch (error) {
      console.error("Failed to refresh configs:", error);
    }
  }, []);

  const loadErrors = useCallback(async () => {
    try {
      const errorList = await getErrors();
      setErrors(errorList);
    } catch (error) {
      console.error("Failed to load errors:", error);
    }
  }, []);

  const handleToggle = useCallback(
    async (configName: string, enabled: boolean) => {
      setLoadingMap((prev) => ({ ...prev, [configName]: true }));
      try {
        const result = enabled
          ? await vpnStartConfig({ config_name: configName })
          : await vpnStopConfig({ config_name: configName });

        if (!result.success) {
          toaster.toast({
            title: "Ошибка",
            body: result.error ?? "Неизвестная ошибка",
          });
          await loadErrors();
        } else {
          toaster.toast({
            title: enabled ? "VPN включён" : "VPN выключен",
            body: configName,
          });
        }
      } catch (error) {
        toaster.toast({
          title: "Ошибка",
          body: String(error),
        });
        await loadErrors();
      } finally {
        setLoadingMap((prev) => ({ ...prev, [configName]: false }));
        await refreshConfigs();
      }
    },
    [refreshConfigs, loadErrors],
  );

  const handleDiagnose = useCallback(async () => {
    setProbesLoading(true);
    try {
      const res = await diagnoseConnectivity();
      setProbes(res);
      const ok = res.filter((p) => p.ok).length;
      toaster.toast({
        title: "Проверка связи",
        body: `${ok}/${res.length} доступно`,
      });
    } catch (e) {
      toaster.toast({ title: "Ошибка диагностики", body: String(e) });
    } finally {
      setProbesLoading(false);
    }
  }, []);

  const handleRepairSymlinks = useCallback(async () => {
    setRepairLoading(true);
    try {
      const res = await repairSymlinks();
      toaster.toast({
        title: "Симлинки",
        body: `Восстановлено ${res.repaired} из ${res.total}`,
      });
      await refreshConfigs();
    } catch (e) {
      toaster.toast({ title: "Ошибка восстановления", body: String(e) });
    } finally {
      setRepairLoading(false);
    }
  }, [refreshConfigs]);

  const handleClearErrors = useCallback(async () => {
    try {
      await clearErrors();
      setErrors([]);
      toaster.toast({ title: "История ошибок очищена", body: "" });
    } catch (error) {
      toaster.toast({ title: "Ошибка очистки", body: String(error) });
    }
  }, []);

  useEffect(() => {
    refreshConfigs();
    loadErrors();

    const configsInterval = setInterval(refreshConfigs, 3000);
    const errorsInterval = setInterval(loadErrors, 10000);

    return () => {
      clearInterval(configsInterval);
      clearInterval(errorsInterval);
    };
  }, [refreshConfigs, loadErrors]);

  return (
    <>
      <PanelSection title="VPN Конфиги">
        {configs.length === 0 && (
          <PanelSectionRow>
            <div style={{ color: "#888", fontSize: "14px" }}>
              Нет импортированных конфигов
            </div>
          </PanelSectionRow>
        )}
        {configs.map((cfg) => {
          const isSub = cfg.kind === "subscription";
          const showStatus = cfg.active && status?.name === cfg.name;
          return (
            <PanelSectionRow key={cfg.interface}>
              <ToggleField
                label={cfg.name}
                description={isSub ? "Подписка" : cfg.label}
                checked={cfg.active}
                disabled={!!loadingMap[cfg.name]}
                onChange={(val) => handleToggle(cfg.name, val)}
                bottomSeparator={isSub || showStatus ? "none" : "standard"}
              />
              {isSub && (
                <SubscriptionServerButton
                  cfg={cfg}
                  onChanged={refreshConfigs}
                  separator={showStatus ? "none" : "standard"}
                />
              )}
              {showStatus && status && <StatusRows status={status} separator="standard" />}
            </PanelSectionRow>
          );
        })}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() =>
              showModal(
                <ImportConfigModal existingNames={configs.map((c) => c.name)} onSuccess={refreshConfigs} />,
              )
            }
          >
            Импортировать конфиг
          </ButtonItem>
        </PanelSectionRow>
        {configs.length > 0 && (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={() => showModal(<DeleteConfigModal configs={configs} onSuccess={refreshConfigs} />)}
            >
              Удалить конфиг…
            </ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>

      <SettingsSection />

      <PanelSection title="Диагностика">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={probesLoading}
            onClick={handleDiagnose}
          >
            {probesLoading ? "Проверка…" : "Проверить связь"}
          </ButtonItem>
        </PanelSectionRow>
        {probes && probes.map((p, i) => (
          <PanelSectionRow key={`${p.name}-${i}`}>
            <div
              style={{
                padding: "8px",
                fontSize: "12px",
                borderLeft: `3px solid ${p.ok ? "#4ade80" : "#f87171"}`,
                paddingLeft: "10px",
                marginBottom: "4px",
              }}
            >
              <div style={{ fontWeight: "bold" }}>
                {p.ok ? "✓" : "✗"} {p.name}
              </div>
              <div style={{ color: "#8b929a" }}>{p.detail}</div>
            </div>
          </PanelSectionRow>
        ))}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={repairLoading}
            onClick={handleRepairSymlinks}
          >
            {repairLoading ? "Восстановление…" : "Восстановить симлинки"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Ошибки">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => {
              setShowErrors(!showErrors);
              if (!showErrors) loadErrors();
            }}
          >
            {showErrors
              ? "Скрыть ошибки"
              : `Просмотр ошибок${errors.length > 0 ? ` (${errors.length})` : ""}`}
          </ButtonItem>
        </PanelSectionRow>

        {showErrors && (
          <>
            {errors.length > 0 && (
              <PanelSectionRow>
                <ButtonItem layout="below" onClick={handleClearErrors}>
                  Очистить историю ошибок
                </ButtonItem>
              </PanelSectionRow>
            )}

            {errors.length === 0 ? (
              <PanelSectionRow>
                <div
                  style={{
                    padding: "10px",
                    textAlign: "center",
                    color: "#888",
                    fontSize: "14px",
                  }}
                >
                  Ошибок не обнаружено
                </div>
              </PanelSectionRow>
            ) : (
              errors
                .slice()
                .reverse()
                .map((error) => (
                  <PanelSectionRow key={`${error.timestamp}-${error.operation}`}>
                    <div
                      style={{
                        padding: "12px",
                        backgroundColor: "rgba(255, 0, 0, 0.1)",
                        borderRadius: "4px",
                        marginBottom: "8px",
                        fontSize: "12px",
                      }}
                    >
                      <div style={{ marginBottom: "4px", fontWeight: "bold" }}>
                        {formatTimestamp(error.timestamp)} - {error.operation}
                      </div>
                      <div style={{ marginBottom: "4px", color: "#FF6B6B" }}>
                        <strong>Тип:</strong> {error.error_type}
                      </div>
                      <div style={{ marginBottom: "4px" }}>
                        <strong>Сообщение:</strong> {error.message}
                      </div>
                      {Object.keys(error.details).length > 0 && (
                        <details style={{ marginTop: "8px" }}>
                          <summary style={{ cursor: "pointer", color: "#888" }}>
                            Детали
                          </summary>
                          <pre
                            style={{
                              marginTop: "4px",
                              padding: "8px",
                              backgroundColor: "rgba(0, 0, 0, 0.3)",
                              borderRadius: "4px",
                              fontSize: "11px",
                              overflow: "auto",
                            }}
                          >
                            {JSON.stringify(error.details, null, 2)}
                          </pre>
                        </details>
                      )}
                    </div>
                  </PanelSectionRow>
                ))
            )}
          </>
        )}
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  console.log("VPN Deck plugin initializing");

  return {
    name: "VPN Deck",
    titleView: <div className={staticClasses.Title}>VPN Deck</div>,
    content: <Content />,
    icon: <FaNetworkWired />,
    onDismount() {
      console.log("VPN Deck plugin unloading");
    },
  };
});
