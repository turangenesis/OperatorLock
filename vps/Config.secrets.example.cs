namespace TGConfig
{
    // ============================================================
    //  CONFIGURATION TEMPLATE — DO NOT COMMIT THE REAL FILE
    //
    //  1. Copy this file to config.secrets.cs (excluded by .gitignore)
    //  2. Replace every REPLACE_ME placeholder with your real values
    //  3. Never commit config.secrets.cs — it contains live credentials
    // ============================================================
    public static partial class Config
    {
        // ============================================================
        //  Rithmic Live Credentials
        //  Your AMP/Rithmic username and password
        // ============================================================
        public const string DefaultRithmicUser     = "REPLACE_ME@yourbroker.com";
        public const string DefaultRithmicPassword = "REPLACE_ME";

        // ============================================================
        //  Bridge → Heroku Authentication
        //  Must match BRIDGE_HEARTBEAT_SECRET in your Heroku config vars
        // ============================================================
        public const string DefaultBridgeHeartbeatSecret = "REPLACE_ME";

        // ============================================================
        //  Trading Defaults
        // ============================================================
        public const string DefaultRithmicExchange = "CME";

        // Symbols to subscribe for market data ticks (optional)
        public static readonly string[] DefaultMdSymbols = new[] { "MESZ5", "MNQZ5" };

        // Heroku endpoint for market data tick forwarding
        public const string DefaultMdTicksEndpoint = "https://YOUR_APP.herokuapp.com/rithmic/md-ticks";
        public const int    DefaultMdMinIntervalSeconds = 1;

        // ============================================================
        //  Heroku Base URL
        //  Used for bridge polling (/api/orders/pending) and posting
        //  execution reports (/api/orders/<id>/execution-report)
        // ============================================================
        public const string DefaultHerokuBaseUrl = "https://YOUR_APP.herokuapp.com";

        // ============================================================
        //  Rithmic Live Connection Parameters
        //  These come from your Rithmic connection sheet.
        //  Do not modify the server addresses unless Rithmic instructs you to.
        // ============================================================
        public const string DefaultDomainName = "rithmic_prod_01_dmz_domain";

        public const string DefaultDmnSrvrAddr =
            "ritpz01001.01.rithmic.com:65000~ritpz01000.01.rithmic.com:65000~" +
            "ritpz01001.01.rithmic.net:65000~ritpz01000.01.rithmic.net:65000~" +
            "ritpz01001.01.theomne.net:65000~ritpz01000.01.theomne.net:65000~" +
            "ritpz01001.01.theomne.com:65000~ritpz01000.01.theomne.com:65000";

        public const string DefaultLicSrvrAddr =
            "ritpz01000.01.rithmic.com:56000~ritpz01001.01.rithmic.com:56000~" +
            "ritpz01000.01.rithmic.net:56000~ritpz01001.01.rithmic.net:56000~" +
            "ritpz01000.01.theomne.net:56000~ritpz01001.01.theomne.net:56000~" +
            "ritpz01000.01.theomne.com:56000~ritpz01001.01.theomne.com:56000~" +
            "ritpz24050.rithmic.com:56000~ritpz24050.rithmic.net:56000~" +
            "ritpz24050.theomne.net:56000~ritpz24050.theomne.com:56000~" +
            "ritpz23010.rithmic.com:56000~ritpz23010.rithmic.net:56000~" +
            "ritpz23010.theomne.net:56000~ritpz23010.theomne.com:56000~" +
            "ritpz23011.rithmic.com:56000~ritpz23011.rithmic.net:56000~" +
            "ritpz23011.theomne.net:56000~ritpz23011.theomne.com:56000~" +
            "ritpz24013.rithmic.com:56000~ritpz24013.rithmic.net:56000~" +
            "ritpz24013.theomne.net:56000~ritpz24013.theomne.com:56000";

        public const string DefaultLocBrokAddr = "ritpz01000.01.rithmic.com:64100";

        public const string DefaultLoggerAddr =
            "ritpz01000.01.rithmic.com:45454~ritpz01000.01.rithmic.net:45454~" +
            "ritpz01000.01.theomne.net:45454~ritpz01000.01.theomne.com:45454";

        // ============================================================
        //  Rithmic Connection Points (LIVE)
        //  Switch from paper connect points to these for live trading
        // ============================================================
        public const string DefaultAdmCnnctPt  = "dd_admin_sslc";
        public const string DefaultRepoCnnctPt  = "login_agent_repositoryc";
        public const string DefaultMdCnnctPt    = "login_agent_tp_r01c";
        public const string DefaultTsCnnctPt    = "login_agent_prodc";
        public const string DefaultPnlCnnctPt   = "login_agent_pnl_sslc";
        public const string DefaultIhCnnctPt    = "login_agent_historyc";

        // ============================================================
        //  Logging
        //  Leave empty to log to stdout only.
        //  Set to a file path (e.g. "orders_bridge.log") to enable file logging.
        // ============================================================
        public const string DefaultOrdersLogFilePath = "";
    }
}
