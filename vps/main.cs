// rithmicbridge.cs (UPDATED) — Orders + Heartbeat + PnL Snapshot Poster
// Notes:
// - Adds Exchange in PendingOrder
// - Builds exchange->tradeRoute mapping for ALL UP routes
// - Routes each order using order.Exchange and its corresponding trade route
// - Keeps existing order polling + execution-report flow intact
// - Keeps PnL snapshot poster + heartbeat logic intact

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using com.omnesys.omne.om; 
using com.omnesys.rapi;
using TGConfig;

namespace RithmicUnifiedBridge
{
    /* ===================================================================== */
    /*  Small logger                                                         */
    /* ===================================================================== */
    static class Log
    {
        public static void Info(string msg) =>
            Console.WriteLine($"[{DateTime.Now:yyyy-MM-dd HH:mm:ss}] {msg}");

        public static void Warn(string msg) =>
            Console.WriteLine($"[{DateTime.Now:yyyy-MM-dd HH:mm:ss}] ⚠️ {msg}");

        public static void Err(string msg) =>
            Console.WriteLine($"[{DateTime.Now:yyyy-MM-dd HH:mm:ss}] ❌ {msg}");
    }

    /* ===================================================================== */
    /*  Admin callbacks                                                      */
    /* ===================================================================== */
    class MyAdmCallbacks : AdmCallbacks
    {
        public override void Alert(AlertInfo info)
        {
            Log.Info($"[ADM] {info.ConnectionId} {info.AlertType} rc={info.RpCode} {info.Message}");
        }
    }

    /* ===================================================================== */
    /*  DTOs for your Heroku polling + heartbeat                              */
    /* ===================================================================== */
    public static class HttpClientSingleton
    {
        public static readonly HttpClient Instance = new HttpClient();
    }

    public class PendingOrdersResponse
    {
        [JsonProperty("ok")]
        public bool Ok { get; set; }

        [JsonProperty("orders")]
        public List<PendingOrder> Orders { get; set; }
    }

    // (A) PendingOrder DTO: add Exchange
    public class PendingOrder
    {
        [JsonProperty("id")]
        public string Id { get; set; }

        [JsonProperty("asset")]
        public string Symbol { get; set; }

        [JsonProperty("exchange")]
        public string Exchange { get; set; }

        [JsonProperty("side")]
        public string Side { get; set; }

        [JsonProperty("qty")]
        public int Quantity { get; set; }

        [JsonProperty("mode")]
        public string Mode { get; set; }

        [JsonProperty("env")]
        public string Env { get; set; }
    }

    public class BridgeHeartbeat
    {
        [JsonProperty("bridge")]
        public string Bridge { get; set; }

        [JsonProperty("secret")]
        public string Secret { get; set; }

        [JsonProperty("version")]
        public string Version { get; set; }

        [JsonProperty("tsConnected")]
        public bool TsConnected { get; set; }

        [JsonProperty("mdConnected")]
        public bool MdConnected { get; set; }

        [JsonProperty("repoOk")]
        public bool RepoOk { get; set; }

        [JsonProperty("account")]
        public string Account { get; set; }

        [JsonProperty("tradeRoute")]
        public string TradeRoute { get; set; }

        [JsonProperty("exchange")]
        public string Exchange { get; set; }

        [JsonProperty("lastRapiEventUtc")]
        public string LastRapiEventUtc { get; set; }

        [JsonProperty("lastOrdersPollUtc")]
        public string LastOrdersPollUtc { get; set; }

        [JsonProperty("lastOrdersOkUtc")]
        public string LastOrdersOkUtc { get; set; }

        [JsonProperty("lastPnlMsgUtc")]
        public string LastPnlMsgUtc { get; set; }

        [JsonProperty("note")]
        public string Note { get; set; }
    }

    /* ===================================================================== */
    /*  PnL snapshot DTOs (NEW)                                               */
    /* ===================================================================== */
    public class PnlSymbolSnapshot
    {
        [JsonProperty("symbol")] public string Symbol { get; set; }
        [JsonProperty("exchange")] public string Exchange { get; set; }
        [JsonProperty("position")] public int Position { get; set; }
        [JsonProperty("openPnl")] public double OpenPnl { get; set; }
        [JsonProperty("avgOpenFill")] public double AvgOpenFill { get; set; }
    }

    public class PnlSnapshotPayload
    {
        [JsonProperty("secret")] public string Secret { get; set; }
        [JsonProperty("account")] public string Account { get; set; }

        [JsonProperty("realized")] public double Realized { get; set; }
        [JsonProperty("unrealized")] public double Unrealized { get; set; }
        [JsonProperty("accountBalance")] public double? AccountBalance { get; set; }

        [JsonProperty("symbols")] public List<PnlSymbolSnapshot> Symbols { get; set; }

        [JsonProperty("tsUtc")] public string TsUtc { get; set; }
    }

    /* ===================================================================== */
    /*  PnL parsing state                                                    */
    /* ===================================================================== */
    class PosState
    {
        public int Position;
        public double OpenPnl;
        public double AvgOpenFill;
        public DateTime EnteredLocal = DateTime.MinValue;
        public long LastPrintMs;
    }

    /* ===================================================================== */
    /*  Unified callbacks: Login + Orders + PnL in ONE                        */
    /* ===================================================================== */
    class UnifiedCallbacks : RCallbacks
    {
        // login state
        public volatile bool RepoLoggedIn = false;
        public volatile bool RepoFailed = false;
        public volatile bool MdLoggedIn = false;
        public volatile bool TsLoggedIn = false;

        // timestamps (thread-safe via Interlocked)
        private long _lastRapiEventUtcMs = 0;
        private long _lastPnlUtcMs = 0;

        public long LastRapiEventUtcMs => Interlocked.Read(ref _lastRapiEventUtcMs);
        public long LastPnlUtcMs => Interlocked.Read(ref _lastPnlUtcMs);

        // account / routes
        public volatile bool GotAccountList = false;
        public AccountInfo PrimaryAccount = null;

        public volatile bool GotTradeRoutes = false;

        // Default exchange for heartbeat display only
        public string Exchange { get; set; } = "CME";

        // Backwards compatibility / heartbeat display
        public string TradeRouteToUse { get; private set; } = "";

        // (B) store trade routes for ALL exchanges
        public Dictionary<string, string> TradeRouteByExchange { get; } =
            new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        // throttling
        private readonly Stopwatch _sw = Stopwatch.StartNew();
        private const int THROTTLE_MS = 1000;

        // account totals
        private long _acctLastPrintMs = 0;
        private double _acctRealized = 0;
        private double _acctUnrealized = 0;
        private double _acctAccountBalance = double.NaN;

        // per-symbol state
        private readonly Dictionary<string, PosState> _pos =
            new Dictionary<string, PosState>(StringComparer.OrdinalIgnoreCase);

        // -------- Snapshot store (thread-safe) (NEW) --------
        private readonly object _snapLock = new object();
        private double _snapRealized = 0;
        private double _snapUnrealized = 0;
        private double? _snapAccountBalance = null;
        private long _snapUtcMs = 0;

        private static long UtcNowMs() => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();

        // returns a deep-copy payload you can POST safely (UPDATED)
        public PnlSnapshotPayload BuildSnapshotForPost(string secret, string accountId)
        {
            lock (_snapLock)
            {
                var list = new List<PnlSymbolSnapshot>();

                foreach (var kv in _pos)
                {
                    var key = kv.Key;
                    var st = kv.Value;

                    var at = key.LastIndexOf('@');
                    string sym = (at > 0) ? key.Substring(0, at) : key;
                    string exch = (at > 0) ? key.Substring(at + 1) : "";

                    list.Add(new PnlSymbolSnapshot
                    {
                        Symbol = sym,
                        Exchange = exch,
                        Position = st.Position,
                        OpenPnl = st.OpenPnl,
                        AvgOpenFill = st.AvgOpenFill
                    });
                }

                long useMs = (_snapUtcMs <= 0) ? UtcNowMs() : _snapUtcMs;

                return new PnlSnapshotPayload
                {
                    Secret = secret,
                    Account = accountId,
                    Realized = _snapRealized,
                    Unrealized = _snapUnrealized,
                    AccountBalance = _snapAccountBalance,
                    Symbols = list,
                    TsUtc = DateTimeOffset.FromUnixTimeMilliseconds(useMs).ToString("o"),
                };
            }
        }

        public override void Alert(AlertInfo oInfo)
        {
            Interlocked.Exchange(ref _lastRapiEventUtcMs, UtcNowMs());

            if (oInfo.AlertType == AlertType.LoginComplete ||
                oInfo.AlertType == AlertType.LoginFailed ||
                oInfo.AlertType == AlertType.ConnectionOpened ||
                oInfo.AlertType == AlertType.ConnectionClosed)
            {
                Log.Info($"[RAPI] {oInfo.ConnectionId} {oInfo.AlertType} rc={oInfo.RpCode} {oInfo.Message}");
            }

            if (oInfo.ConnectionId == ConnectionId.Repository)
            {
                if (oInfo.AlertType == AlertType.LoginComplete) RepoLoggedIn = true;
                if (oInfo.AlertType == AlertType.LoginFailed) RepoFailed = true;
            }

            if (oInfo.AlertType == AlertType.LoginComplete && oInfo.ConnectionId == ConnectionId.MarketData)
                MdLoggedIn = true;

            if (oInfo.AlertType == AlertType.LoginComplete && oInfo.ConnectionId == ConnectionId.TradingSystem)
                TsLoggedIn = true;
        }

        public override void AccountList(AccountListInfo oInfo)
        {
            GotAccountList = true;

            if (oInfo?.Accounts == null || oInfo.Accounts.Count == 0)
            {
                Log.Err("AccountList returned 0 accounts.");
                return;
            }

            PrimaryAccount = oInfo.Accounts[0];
            Log.Info($"AccountList received. Using first account: FCM={PrimaryAccount.FcmId} IB={PrimaryAccount.IbId} ACCT={PrimaryAccount.AccountId}");
        }

        // (B) replace TradeRouteList body with exchange->route map builder
        public override void TradeRouteList(TradeRouteListInfo oInfo)
        {
            GotTradeRoutes = true;

            if (oInfo?.TradeRoutes == null || oInfo.TradeRoutes.Count == 0 || PrimaryAccount == null)
            {
                Log.Err("TradeRouteList empty or Account missing.");
                return;
            }

            // Build routes for any exchange that has an UP route for this account
            TradeRouteByExchange.Clear();

            foreach (var tr in oInfo.TradeRoutes)
            {
                if (tr.FcmId != PrimaryAccount.FcmId) continue;
                if (tr.IbId != PrimaryAccount.IbId) continue;
                if (tr.Status != Constants.TRADE_ROUTE_STATUS_UP) continue;

                // Keep first UP route per exchange (good enough for now)
                if (!TradeRouteByExchange.ContainsKey(tr.Exchange))
                    TradeRouteByExchange[tr.Exchange] = tr.TradeRoute;
            }

            // For logging: show the ones you care about
            foreach (var exch in new[] { "CME", "CBOT", "COMEX" })
            {
                if (TradeRouteByExchange.TryGetValue(exch, out var route))
                    Log.Info($"TradeRoute UP: {exch} -> {route}");
                else
                    Log.Warn($"No UP trade route found for exchange={exch}");
            }

            // Keep this for backwards compatibility / heartbeat display
            TradeRouteToUse = TradeRouteByExchange.TryGetValue(Exchange, out var defRoute) ? defRoute : "";
        }

        /* ===================== ORDER REPORTS ===================== */

        public override void StatusReport(OrderStatusReport oReport)
        {
            string id = oReport?.Tag;
            if (string.IsNullOrEmpty(id)) return;

            Log.Info($"ORDER WORKING id={id} sym={oReport.Symbol} exch={oReport.Exchange} side={oReport.BuySellType}");
            _ = Program.SendExecutionReport(id, "WORKING", null, DumpSmall(oReport));
        }

        public override void FillReport(OrderFillReport oReport)
        {
            string id = oReport?.Tag;
            if (string.IsNullOrEmpty(id)) return;

            double fillPrice = oReport.FillPrice;
            Log.Info($"ORDER FILLED  id={id} sym={oReport.Symbol} exch={oReport.Exchange} price={fillPrice}");
            _ = Program.SendExecutionReport(id, "FILLED", fillPrice, DumpSmall(oReport));
        }

        public override void RejectReport(OrderRejectReport oReport)
        {
            string id = oReport?.Tag;
            if (string.IsNullOrEmpty(id)) return;

            Log.Err($"ORDER REJECTED id={id}");
            _ = Program.SendExecutionReport(id, "REJECTED", null, DumpSmall(oReport));
        }

        public override void FailureReport(OrderFailureReport oReport)
        {
            string id = oReport?.Tag;
            if (string.IsNullOrEmpty(id)) return;

            Log.Err($"ORDER FAILED   id={id}");
            _ = Program.SendExecutionReport(id, "REJECTED", null, DumpSmall(oReport));
        }

        public override void CancelReport(OrderCancelReport oReport)
        {
            string id = oReport?.Tag;
            if (string.IsNullOrEmpty(id)) return;

            Log.Warn($"ORDER CANCELLED id={id}");
            _ = Program.SendExecutionReport(id, "CANCELLED", null, DumpSmall(oReport));
        }

        private static string DumpSmall(object report) => report?.ToString() ?? "";

        /* ===================== PnL CALLBACKS ===================== */

        public override void PnlReplay(PnlReplayInfo oInfo)
        {
            Interlocked.Exchange(ref _lastPnlUtcMs, UtcNowMs());
            var sb = new StringBuilder();
            oInfo.Dump(sb);
            HandlePnlDump(sb.ToString(), isReplay: true);
        }

        public override void PnlUpdate(PnlInfo oInfo)
        {
            Interlocked.Exchange(ref _lastPnlUtcMs, UtcNowMs());
            var sb = new StringBuilder();
            oInfo.Dump(sb);
            HandlePnlDump(sb.ToString(), isReplay: false);
        }

        private void HandlePnlDump(string dump, bool isReplay)
        {
            var map = ParseDumpKeyValues(dump);

            string symbol = GetStr(map, "Symbol");
            string exch = GetStr(map, "Exchange");

            double ts = GetDouble(map, "Timestamp");
            DateTime tsLocal = (ts > 0) ? UnixSecondsToLocal(ts) : DateTime.Now;

            // Account totals update
            if (string.IsNullOrWhiteSpace(symbol))
            {
                _acctUnrealized = GetDouble(map, "Open PnL");
                _acctRealized = GetDouble(map, "Closed PnL");

                double acctBal = GetDouble(map, "Account Balance");
                if (!double.IsNaN(acctBal)) _acctAccountBalance = acctBal;

                lock (_snapLock)
                {
                    _snapUnrealized = double.IsNaN(_acctUnrealized) ? 0.0 : _acctUnrealized;
                    _snapRealized = double.IsNaN(_acctRealized) ? 0.0 : _acctRealized;
                    _snapAccountBalance = double.IsNaN(_acctAccountBalance) ? (double?)null : _acctAccountBalance;
                    _snapUtcMs = UtcNowMs();
                }

                PrintAccountIfDue(tsLocal);
                return;
            }

            // Symbol-level update
            int pos = GetInt(map, "Position");
            double openPnl = GetDouble(map, "Open PnL");
            double avg = GetDouble(map, "Avg Open Fill Price");

            // --- locals for safe logging outside lock ---
            bool doLog = false;
            int logPos = 0;
            double logOpen = 0;
            double logAvg = 0;
            DateTime logEntered = DateTime.MinValue;
            bool logReplay = isReplay;
            string logSym = symbol;
            string logExch = exch;

            lock (_snapLock)
            {
                string key = $"{symbol}@{exch}";
                if (!_pos.TryGetValue(key, out var st))
                {
                    st = new PosState();
                    _pos[key] = st;
                }

                if (st.Position == 0 && pos != 0)
                    st.EnteredLocal = tsLocal;

                if (pos == 0)
                    st.EnteredLocal = DateTime.MinValue;

                st.Position = pos;
                st.OpenPnl = openPnl;
                st.AvgOpenFill = avg;

                _snapUtcMs = UtcNowMs();

                // throttle decision + LastPrintMs update MUST be inside lock
                if (st.Position != 0)
                {
                    long nowSw = _sw.ElapsedMilliseconds;
                    if (nowSw - st.LastPrintMs >= THROTTLE_MS)
                    {
                        st.LastPrintMs = nowSw;

                        doLog = true;
                        logPos = st.Position;
                        logOpen = st.OpenPnl;
                        logAvg = st.AvgOpenFill;
                        logEntered = st.EnteredLocal;
                    }
                }
            }

            // logging outside lock, using captured primitives only
            if (doLog)
            {
                string entered = (logEntered == DateTime.MinValue) ? "?" : logEntered.ToString("yyyy-MM-dd HH:mm:ss");
                string tag = logReplay ? "REPLAY" : "LIVE";
                Log.Info($"{tag} PNL {logSym} {logExch} Pos={logPos} OpenPnL={logOpen:0.##} Avg={logAvg:0.##} Entered≈{entered}");
            }
        }

        private void PrintAccountIfDue(DateTime tsLocal)
        {
            long now = _sw.ElapsedMilliseconds;
            if (now - _acctLastPrintMs < THROTTLE_MS) return;
            _acctLastPrintMs = now;

            string bal = double.IsNaN(_acctAccountBalance) ? "?" : _acctAccountBalance.ToString("0.##", CultureInfo.InvariantCulture);
            Log.Info($"ACCOUNT Realized={_acctRealized:0.##} Unrealized={_acctUnrealized:0.##} AccountBalance={bal}");
        }

        // -------- Dump parsing helpers --------

        private static Dictionary<string, string> ParseDumpKeyValues(string dump)
        {
            var dict = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            var lines = dump.Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries);

            foreach (var raw in lines)
            {
                var line = raw.Trim();
                int idx = line.IndexOf(':');
                if (idx <= 0) continue;

                var key = line.Substring(0, idx).Trim();
                var val = line.Substring(idx + 1).Trim();

                int paren = val.IndexOf('(');
                if (paren > 0) val = val.Substring(0, paren).Trim();

                if (!dict.ContainsKey(key))
                    dict[key] = val;
            }

            return dict;
        }

        private static string GetStr(Dictionary<string, string> map, string key) =>
            map.TryGetValue(key, out var v) ? (v ?? "").Trim() : "";

        private static double GetDouble(Dictionary<string, string> map, string key)
        {
            if (!map.TryGetValue(key, out var v)) return double.NaN;
            v = (v ?? "").Trim();
            if (v.Length == 0) return double.NaN;

            if (double.TryParse(v, NumberStyles.Any, CultureInfo.InvariantCulture, out var d))
                return d;

            if (double.TryParse(v, NumberStyles.Any, CultureInfo.CurrentCulture, out d))
                return d;

            return double.NaN;
        }

        private static int GetInt(Dictionary<string, string> map, string key)
        {
            if (!map.TryGetValue(key, out var v)) return 0;
            v = (v ?? "").Trim();
            if (v.Length == 0) return 0;

            if (int.TryParse(v, NumberStyles.Any, CultureInfo.InvariantCulture, out var i))
                return i;

            if (double.TryParse(v, NumberStyles.Any, CultureInfo.InvariantCulture, out var d))
                return (int)Math.Round(d);

            return 0;
        }

        private static DateTime UnixSecondsToLocal(double unixSeconds)
        {
            long whole = (long)Math.Floor(unixSeconds);
            double frac = unixSeconds - whole;
            long ms = (long)Math.Round(frac * 1000.0);

            var dto = DateTimeOffset.FromUnixTimeSeconds(whole).AddMilliseconds(ms);
            return dto.ToLocalTime().DateTime;
        }
    }

    /* ===================================================================== */
    /*  Program: ONE engine, ONE login, run Orders polling + PnL together     */
    /* ===================================================================== */
    class Program
    {
        private static readonly string HEROKU_BASE_URL = Config.HerokuBaseUrl;
        //private static readonly string PENDING_ORDERS_URL = HEROKU_BASE_URL + "/api/orders/pending";
        private static readonly string EXEC_REPORT_URL_TEMPLATE = HEROKU_BASE_URL + "/api/orders/{0}/execution-report";

        private static readonly string HEARTBEAT_URL = HEROKU_BASE_URL + "/api/bridge/heartbeat";

        // NEW: PnL snapshot endpoint
        private static readonly string PNL_SNAPSHOT_URL = HEROKU_BASE_URL + "/api/rithmic/pnl-snapshot";

        private static REngine _engine;
        private static UnifiedCallbacks _cb;
        private static CancellationTokenSource _cts;

        // orders polling timestamps for heartbeat (thread-safe via Interlocked)
        private static long _lastOrdersPollUtcMs = 0;
        private static long _lastOrdersOkUtcMs = 0;

        private static long UtcNowMs() => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        private static string MsToIso(long ms) => (ms <= 0) ? null : DateTimeOffset.FromUnixTimeMilliseconds(ms).ToString("o");

        private static string BuildPendingOrdersUrl()
        {
            var owner = $"bridge-{Environment.MachineName}-{Process.GetCurrentProcess().Id}";
            return $"{HEROKU_BASE_URL}/api/orders/pending" +
                   $"?limit=10" +
                   $"&secret={Uri.EscapeDataString(Config.BridgeHeartbeatSecret)}" +
                   $"&owner={Uri.EscapeDataString(owner)}";
        }


        static void Main(string[] args)
        {
            _cts = new CancellationTokenSource();

            Console.CancelKeyPress += (s, e) =>
            {
                e.Cancel = true;
                Log.Warn("CTRL+C received. Shutting down...");
                _cts.Cancel();
            };

            try
            {
                ConnectSingleEngine();
                RunLoops(_cts.Token).GetAwaiter().GetResult();
            }
            catch (Exception ex)
            {
                Log.Err($"Fatal: {ex.Message}");
            }
            finally
            {
                Shutdown();
            }
        }

        private static void ConnectSingleEngine()
        {
            _cb = new UnifiedCallbacks();

            var p = new REngineParams
            {
                AppName = "emtu:EmreTuranRAPI",
                AppVersion = "1.0.0",
                AdmCallbacks = new MyAdmCallbacks(),

                // Use config values (keeps env switches easy)
                DomainName = Config.DomainName,
                DmnSrvrAddr = Config.DmnSrvrAddr,
                LicSrvrAddr = Config.LicSrvrAddr,
                LocBrokAddr = Config.LocBrokAddr,
                LoggerAddr = Config.LoggerAddr,

                LogFilePath = Config.OrdersLogFilePath
            };

            Log.Info("Creating REngine...");
            _engine = new REngine(p);

            Log.Info("Logging into repository...");
            _engine.loginRepository(
                _cb,
                "",
                Config.RithmicUser,
                Config.RithmicPassword,
                "login_agent_repositoryc"
            );

            Wait(() => _cb.RepoLoggedIn || _cb.RepoFailed, 30_000, "Repo login");
            if (_cb.RepoFailed) throw new Exception("Repository login failed.");
            _engine.logoutRepository();

            Log.Info("Logging in main (MD+TS+PnL+IH)...");
            _engine.login(
                _cb,
                "",
                // MD connect point (LIVE)
                Config.RithmicUser, Config.RithmicPassword, "login_agent_tp_r01c",
                Constants.DEFAULT_ENVIRONMENT_KEY,

                // TS connect point (LIVE)
                Config.RithmicUser, Config.RithmicPassword, "login_agent_prodc",

                // PnL connect point (LIVE)
                "login_agent_pnl_sslc",

                Constants.DEFAULT_ENVIRONMENT_KEY,

                // Intraday History connect point (LIVE)
                Config.RithmicUser, Config.RithmicPassword, "login_agent_historyc"
            );


            Wait(() => _cb.MdLoggedIn && _cb.TsLoggedIn, 120_000, "Login MD/TS");

            Log.Info("Waiting for AccountList...");
            Wait(() => _cb.GotAccountList, 20_000, "AccountList");
            if (_cb.PrimaryAccount == null) throw new Exception("No account returned from AccountList.");

            Log.Info("Subscribing to orders + PnL...");
            _engine.subscribeOrder(_cb.PrimaryAccount);
            _engine.subscribePnl(_cb.PrimaryAccount);
            _engine.replayPnl(_cb.PrimaryAccount, null);

            // (C) stop assuming only one exchange route
            _cb.Exchange = Config.RithmicExchange; // default only (for heartbeat display)
            Log.Info($"Listing trade routes (default exch={_cb.Exchange})...");
            _engine.listTradeRoutes(null);
            Wait(() => _cb.GotTradeRoutes, 20_000, "TradeRouteList");

            // Require / warn the exchanges you intend to trade
            foreach (var required in new[] { "CME", "CBOT", "COMEX" })
            {
                if (!_cb.TradeRouteByExchange.ContainsKey(required))
                    Log.Warn($"⚠️ Missing UP trade route for {required}. Orders on that exchange may reject.");
            }

            Log.Info("✅ Unified engine ready: Orders + PnL running under ONE session.");
        }

        private static async Task RunLoops(CancellationToken token)
        {
            var ordersTask = Task.Run(() => OrdersPollingLoop(token), token);
            var hbTask = Task.Run(() => HeartbeatLoop(token), token);

            while (!token.IsCancellationRequested)
                await Task.Delay(500, token);

            try { await ordersTask; } catch { }
            try { await hbTask; } catch { }
        }

        private static async Task OrdersPollingLoop(CancellationToken token)
        {
            while (!token.IsCancellationRequested)
            {
                try
                {
                    var pendingOrders = await GetPendingOrdersAsync();
                    if (pendingOrders.Count > 0)
                        Log.Info($"Pending orders fetched: {pendingOrders.Count}");

                    foreach (var order in pendingOrders)
                        SendMarketOrder(order);
                }
                catch (Exception ex)
                {
                    Log.Err($"Orders loop error: {ex.Message}");
                }

                await Task.Delay(1000, token);
            }
        }

        private static async Task HeartbeatLoop(CancellationToken token)
        {
            const int PRINT_EVERY_MS = 2000;
            const int HEARTBEAT_POST_EVERY_MS = 10000;

            // PnL snapshot POST cadence (you want 5s)
            const int PNL_POST_EVERY_MS = 2000;

            // ✅ NEW: if no PnL messages for 20s, request replay (but at most once per 60s)
            const int PNL_STALE_MS = 20000;
            const int PNL_REPLAY_MIN_GAP_MS = 60000;

            const bool POST_TO_HEROKU = true;

            long lastPrint = 0;
            long lastHeartbeatPost = 0;
            long lastPnlPost = 0;
            long lastPnlReplay = 0;

            while (!token.IsCancellationRequested)
            {
                var now = UtcNowMs();

                // -----------------------------
                // Console print every 5s
                // -----------------------------
                if (now - lastPrint >= PRINT_EVERY_MS)
                {
                    lastPrint = now;

                    long lastRapi = _cb?.LastRapiEventUtcMs ?? 0;
                    long lastPnl = _cb?.LastPnlUtcMs ?? 0;

                    bool ts = _cb?.TsLoggedIn ?? false;
                    bool md = _cb?.MdLoggedIn ?? false;

                    string note = "";
                    if (ts && lastRapi > 0 && (now - lastRapi) > 60_000)
                        note = "STALE: no RAPI events >60s (NOT reconnecting)";

                    Log.Info(
                        $"HB alive ts={ts} md={md} repoOk={(_cb != null && !_cb.RepoFailed)} " +
                        $"acct={_cb?.PrimaryAccount?.AccountId ?? "?"} route={_cb?.TradeRouteToUse ?? "?"} " +
                        $"lastRapi={MsToIso(lastRapi) ?? "?"} lastPnl={MsToIso(lastPnl) ?? "?"} " +
                        $"ordersPoll={MsToIso(Interlocked.Read(ref _lastOrdersPollUtcMs)) ?? "?"} " +
                        $"ordersOk={MsToIso(Interlocked.Read(ref _lastOrdersOkUtcMs)) ?? "?"} " +
                        $"{note}"
                    );
                }

                // -----------------------------
                // ✅ NEW: Force PnL refresh when quiet/flat
                // -----------------------------
                if (_engine != null && _cb?.PrimaryAccount != null)
                {
                    long lastPnl = _cb.LastPnlUtcMs;

                    bool pnlNeverSeen = (lastPnl <= 0);
                    bool pnlStale = (!pnlNeverSeen && (now - lastPnl) >= PNL_STALE_MS);

                    if ((pnlStale || pnlNeverSeen) && (now - lastPnlReplay) >= PNL_REPLAY_MIN_GAP_MS)
                    {
                        lastPnlReplay = now;
                        try
                        {
                            Log.Info($"PnL quiet/stale. Triggering replayPnl() to resync account snapshot...");
                            _engine.replayPnl(_cb.PrimaryAccount, null);
                        }
                        catch (Exception ex)
                        {
                            Log.Warn($"replayPnl failed: {ex.Message}");
                        }
                    }
                }

                // -----------------------------
                // PnL snapshot POST every 5s
                // -----------------------------
                if (POST_TO_HEROKU && now - lastPnlPost >= PNL_POST_EVERY_MS)
                {
                    lastPnlPost = now;
                    try
                    {
                        var acct = _cb?.PrimaryAccount?.AccountId;
                        if (!string.IsNullOrWhiteSpace(acct))
                        {
                            var snap = _cb.BuildSnapshotForPost(Config.BridgeHeartbeatSecret, acct);
                            await PostPnlSnapshotAsync(snap);
                        }
                    }
                    catch (Exception ex)
                    {
                        Log.Warn($"PnL snapshot POST failed: {ex.Message}");
                    }
                }

                // -----------------------------
                // Heartbeat POST every 10s
                // -----------------------------
                if (POST_TO_HEROKU && now - lastHeartbeatPost >= HEARTBEAT_POST_EVERY_MS)
                {
                    lastHeartbeatPost = now;
                    try
                    {
                        var hb = BuildHeartbeatPayload();
                        await PostHeartbeatAsync(hb);
                    }
                    catch (Exception ex)
                    {
                        Log.Warn($"Heartbeat POST failed: {ex.Message}");
                    }
                }

                await Task.Delay(250, token);
            }
        }

        private static BridgeHeartbeat BuildHeartbeatPayload()
        {
            return new BridgeHeartbeat
            {
                Secret = Config.BridgeHeartbeatSecret,
                Bridge = "rithmic-unified-bridge",
                Version = "v1",
                TsConnected = _cb?.TsLoggedIn ?? false,
                MdConnected = _cb?.MdLoggedIn ?? false,
                RepoOk = (_cb != null && !_cb.RepoFailed),
                Account = _cb?.PrimaryAccount?.AccountId,
                TradeRoute = _cb?.TradeRouteToUse,
                Exchange = _cb?.Exchange,
                LastRapiEventUtc = MsToIso(_cb?.LastRapiEventUtcMs ?? 0),
                LastOrdersPollUtc = MsToIso(Interlocked.Read(ref _lastOrdersPollUtcMs)),
                LastOrdersOkUtc = MsToIso(Interlocked.Read(ref _lastOrdersOkUtcMs)),
                LastPnlMsgUtc = MsToIso(_cb?.LastPnlUtcMs ?? 0),
                Note = null
            };
        }

        private static async Task PostHeartbeatAsync(BridgeHeartbeat hb)
        {
            var client = HttpClientSingleton.Instance;
            string json = JsonConvert.SerializeObject(hb);
            var content = new StringContent(json, Encoding.UTF8, "application/json");

            var resp = await client.PostAsync(HEARTBEAT_URL, content);
            if (!resp.IsSuccessStatusCode)
                Log.Warn($"Heartbeat POST HTTP {(int)resp.StatusCode} {resp.ReasonPhrase}");
        }

        // NEW: PnL Snapshot POST helper
        private static async Task PostPnlSnapshotAsync(PnlSnapshotPayload payload)
        {
            var client = HttpClientSingleton.Instance;
            string json = JsonConvert.SerializeObject(payload);
            var content = new StringContent(json, Encoding.UTF8, "application/json");

            var resp = await client.PostAsync(PNL_SNAPSHOT_URL, content);
            if (!resp.IsSuccessStatusCode)
                Log.Warn($"PnL snapshot POST HTTP {(int)resp.StatusCode} {resp.ReasonPhrase}");
        }

        // (D) SendMarketOrder(): use order.Exchange + exchange-specific trade route
        private static void SendMarketOrder(PendingOrder order)
        {
            if (order == null || string.IsNullOrWhiteSpace(order.Id)) return;
            if (_cb.PrimaryAccount == null) return;

            string exch = (order.Exchange ?? "").Trim().ToUpperInvariant();
            if (string.IsNullOrWhiteSpace(exch))
                exch = _cb.Exchange ?? "CME";

            if (!_cb.TradeRouteByExchange.TryGetValue(exch, out var route) || string.IsNullOrWhiteSpace(route))
            {
                Log.Err($"No trade route available for exchange={exch}. Rejecting order id={order.Id}");
                _ = SendExecutionReport(order.Id, "REJECTED", null, $"No trade route for exchange={exch}");
                return;
            }

            string sideUpper = (order.Side ?? "").Trim().ToUpperInvariant();
            string buySellType =
                (sideUpper.StartsWith("S"))
                    ? Constants.BUY_SELL_TYPE_SELL
                    : Constants.BUY_SELL_TYPE_BUY;

            var o = new MarketOrderParams
            {
                Account = _cb.PrimaryAccount,
                BuySellType = buySellType,
                Context = null,
                Duration = Constants.ORDER_DURATION_DAY,
                EntryType = Constants.ORDER_ENTRY_TYPE_MANUAL,

                Exchange = exch,
                TradeRoute = route,

                Qty = order.Quantity,
                Symbol = order.Symbol,
                Tag = order.Id,
                UserMsg = order.Id,
            };

            Log.Info($"SEND ORDER id={order.Id} exch={exch} route={route} sym={order.Symbol} side={buySellType} qty={order.Quantity}");

            try
            {
                _engine.sendOrder(o);
            }
            catch (OMException ex)
            {
                Log.Err($"sendOrder OMException id={order.Id}: {ex.Message}");
                _ = SendExecutionReport(order.Id, "REJECTED", null, ex.Message);
            }
            catch (Exception ex)
            {
                Log.Err($"sendOrder Exception id={order.Id}: {ex.Message}");
                _ = SendExecutionReport(order.Id, "REJECTED", null, ex.Message);
            }
        }

        private static async Task<List<PendingOrder>> GetPendingOrdersAsync()
        {
            Interlocked.Exchange(ref _lastOrdersPollUtcMs, UtcNowMs());

            var client = HttpClientSingleton.Instance;

            HttpResponseMessage resp;
            try
            {
               // resp = await client.GetAsync(PENDING_ORDERS_URL);
                var url = BuildPendingOrdersUrl();
                resp = await client.GetAsync(url);

            }
            catch
            {
                return new List<PendingOrder>();
            }

            if (!resp.IsSuccessStatusCode)
                return new List<PendingOrder>();

            Interlocked.Exchange(ref _lastOrdersOkUtcMs, UtcNowMs());

            var json = await resp.Content.ReadAsStringAsync();
            var wrapper = JsonConvert.DeserializeObject<PendingOrdersResponse>(json);

            if (wrapper == null || !wrapper.Ok || wrapper.Orders == null)
                return new List<PendingOrder>();

            return wrapper.Orders;
        }

        public static async Task SendExecutionReport(string orderId, string status, double? fillPrice, string raw)
        {
            if (string.IsNullOrEmpty(orderId)) return;

            var client = HttpClientSingleton.Instance;

            var payload = new
            {
                status = status,
                extra = new
                {
                    fill_price = fillPrice,
                    raw = raw,
                    source = "rithmic"
                }
            };

            string url = string.Format(EXEC_REPORT_URL_TEMPLATE, orderId);

            try
            {
                string json = JsonConvert.SerializeObject(payload);
                var content = new StringContent(json, Encoding.UTF8, "application/json");
                await client.PostAsync(url, content);
            }
            catch { }
        }

        private static void Shutdown()
        {
            try { _engine?.logout(); } catch { }
            try { _engine?.shutdown(); } catch { }
            Log.Info("Shutdown complete.");
        }

        private static void Wait(Func<bool> cond, int timeoutMs, string label)
        {
            int start = Environment.TickCount;
            while (!cond())
            {
                Thread.Sleep(200);
                if (Environment.TickCount - start > timeoutMs)
                    throw new Exception($"Timeout waiting for: {label}");
            }
            Log.Info($"OK: {label}");
        }
    }
}

