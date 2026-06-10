//+------------------------------------------------------------------+
//| Vantage_BB_Bot_XAGUSD_v9.00.mq5                                  |
//| XAGUSD専用 BB Hedge Bot                                           |
//| TradingView → Flask → MT5ポーリング方式                           |
//| MagicNumber: 20250615                                             |
//+------------------------------------------------------------------+
#property copyright "BB Hedge Bot"
#property version   "9.00"
#property strict

#include <Trade\Trade.mqh>

//============================================================
// 入力パラメータ
//============================================================
input string   PollURL        = "https://st-master-bot.onrender.com/mt5poll"; // ポーリングURL
input string   WebhookSecret  = "bitget_master_bot";  // 認証シークレット
input string   ManagedSymbol  = "XAGUSD";             // 管理銘柄
input long     MagicNumber    = 20250615;              // ★XAGUSD専用Magic
input double   LotSize        = 0.01;                 // ロットサイズ
input int      PollInterval   = 5;                    // ポーリング間隔（秒）
input int      Slippage        = 30;                  // スリッページ（ポイント）

// 損切り設定
input bool     UseSL          = true;                 // SLを使用するか
input double   StopLossUSD    = 30.0;                 // 緊急強制損切りUSD（保険）
input double   MinSL_Distance = 200;                  // 最小SL距離（ポイント）

// 建値移動設定（Phase1）
input double   Phase1_Profit  = 5.0;                  // 建値移動発動利益USD（0で無効）

//============================================================
// グローバル変数
//============================================================
CTrade trade;
datetime lastPollTime = 0;
bool     phase1Done   = false;

//+------------------------------------------------------------------+
//| Expert initialization                                            |
//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(Slippage);
   trade.SetTypeFilling(GetFillingMode());
   EventSetTimer(PollInterval);
   Print("✅ XAGUSD EA v9.00 起動 Magic=", MagicNumber);
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Expert deinitialization                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   Print("🛑 XAGUSD EA 停止");
}

//+------------------------------------------------------------------+
//| Timer：ポーリング                                                 |
//+------------------------------------------------------------------+
void OnTimer()
{
   PollAndExecute();
}

//+------------------------------------------------------------------+
//| Tick：緊急損切り監視                                              |
//+------------------------------------------------------------------+
void OnTick()
{
   // 強制損切り（保険）
   if(StopLossUSD > 0)
   {
      CheckEmergencyStop(POSITION_TYPE_BUY);
      CheckEmergencyStop(POSITION_TYPE_SELL);
   }

   // Phase1：建値移動
   if(Phase1_Profit > 0)
   {
      CheckPhase1(POSITION_TYPE_BUY);
      CheckPhase1(POSITION_TYPE_SELL);
   }
}

//+------------------------------------------------------------------+
//| ポーリング＆注文実行                                              |
//+------------------------------------------------------------------+
void PollAndExecute()
{
   string url     = PollURL + "?secret=" + WebhookSecret + "&symbol=" + ManagedSymbol;
   string headers = "Content-Type: application/json\r\n";
   char   postData[];
   char   result[];
   string responseHeaders;

   int res = WebRequest("GET", url, headers, 5000, postData, result, responseHeaders);
   if(res < 0)
   {
      Print("❌ WebRequest失敗 error=", GetLastError());
      return;
   }

   string response = CharArrayToString(result);
   string status   = ExtractJson(response, "status");

   if(status != "order") return;

   string action = ExtractJson(response, "action");
   string symbol = ExtractJson(response, "symbol");

   if(symbol != ManagedSymbol)
   {
      Print("⚠️ シンボル不一致: ", symbol, " / 管理対象: ", ManagedSymbol);
      return;
   }

   Print("📩 シグナル受信: action=", action, " symbol=", symbol);

   if(action == "long")        ExecuteEntry(ORDER_TYPE_BUY);
   else if(action == "short")  ExecuteEntry(ORDER_TYPE_SELL);
   else if(action == "close_long")  CloseAllPositions(POSITION_TYPE_BUY);
   else if(action == "close_short") CloseAllPositions(POSITION_TYPE_SELL);
}

//+------------------------------------------------------------------+
//| エントリー実行                                                    |
//+------------------------------------------------------------------+
void ExecuteEntry(ENUM_ORDER_TYPE orderType)
{
   double price = (orderType == ORDER_TYPE_BUY)
                  ? SymbolInfoDouble(ManagedSymbol, SYMBOL_ASK)
                  : SymbolInfoDouble(ManagedSymbol, SYMBOL_BID);

   double sl = 0.0;
   if(UseSL)
   {
      double point    = SymbolInfoDouble(ManagedSymbol, SYMBOL_POINT);
      double slPoints = MathMax(MinSL_Distance, 200.0);
      sl = (orderType == ORDER_TYPE_BUY)
           ? price - slPoints * point
           : price + slPoints * point;
   }

   MqlTradeRequest req = {};
   MqlTradeResult  resp = {};

   req.action       = TRADE_ACTION_DEAL;
   req.symbol       = ManagedSymbol;
   req.volume       = LotSize;
   req.type         = orderType;
   req.price        = price;
   req.sl           = sl;
   req.deviation    = Slippage;
   req.magic        = MagicNumber;
   req.comment      = "BB_XAGUSD_v9";
   req.type_filling = GetFillingMode();

   bool sent = OrderSend(req, resp);
   if(sent && resp.retcode == TRADE_RETCODE_DONE)
      Print("✅ エントリー成功: ", (orderType == ORDER_TYPE_BUY ? "BUY" : "SELL"),
            " price=", resp.price, " lot=", LotSize);
   else
      Print("❌ エントリー失敗 retcode=", resp.retcode);

   phase1Done = false;
}

//+------------------------------------------------------------------+
//| 全ポジション決済                                                  |
//+------------------------------------------------------------------+
void CloseAllPositions(long posType)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != ManagedSymbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      if(PositionGetInteger(POSITION_TYPE)  != posType) continue;

      MqlTradeRequest req = {};
      MqlTradeResult  resp = {};

      req.action       = TRADE_ACTION_DEAL;
      req.symbol       = ManagedSymbol;
      req.volume       = PositionGetDouble(POSITION_VOLUME);
      req.type         = (posType == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
      req.price        = (posType == POSITION_TYPE_BUY)
                         ? SymbolInfoDouble(ManagedSymbol, SYMBOL_BID)
                         : SymbolInfoDouble(ManagedSymbol, SYMBOL_ASK);
      req.deviation    = Slippage;
      req.magic        = MagicNumber;
      req.position     = ticket;
      req.type_filling = GetFillingMode();

      bool sent = OrderSend(req, resp);
      if(sent && resp.retcode == TRADE_RETCODE_DONE)
         Print("✅ 決済成功 ticket=", ticket);
      else
         Print("❌ 決済失敗 retcode=", resp.retcode);
   }
   phase1Done = false;
}

//+------------------------------------------------------------------+
//| 緊急強制損切り（保険）                                            |
//+------------------------------------------------------------------+
void CheckEmergencyStop(long posType)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != ManagedSymbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      if(PositionGetInteger(POSITION_TYPE)  != posType) continue;

      double profit = PositionGetDouble(POSITION_PROFIT);
      if(profit <= -StopLossUSD)
      {
         Print("🚨 緊急損切り発動 profit=", profit, " USD");
         CloseAllPositions(posType);
      }
   }
}

//+------------------------------------------------------------------+
//| Phase1：建値移動                                                  |
//+------------------------------------------------------------------+
void CheckPhase1(long posType)
{
   if(phase1Done) return;

   double totalProfit = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != ManagedSymbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      if(PositionGetInteger(POSITION_TYPE)  != posType) continue;
      totalProfit += PositionGetDouble(POSITION_PROFIT);
   }

   if(totalProfit < Phase1_Profit) return;

   // 建値にSLを移動
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != ManagedSymbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      if(PositionGetInteger(POSITION_TYPE)  != posType) continue;

      double openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
      double currentSL = PositionGetDouble(POSITION_SL);

      bool shouldMove = (posType == POSITION_TYPE_BUY  && (currentSL < openPrice || currentSL == 0)) ||
                        (posType == POSITION_TYPE_SELL && (currentSL > openPrice || currentSL == 0));

      if(shouldMove)
      {
         trade.PositionModify(ticket, openPrice, PositionGetDouble(POSITION_TP));
         Print("🔒 建値移動 ticket=", ticket, " SL→", openPrice);
      }
   }
   phase1Done = true;
}

//+------------------------------------------------------------------+
//| FillingMode自動取得                                               |
//+------------------------------------------------------------------+
ENUM_ORDER_TYPE_FILLING GetFillingMode()
{
   uint filling = (uint)SymbolInfoInteger(ManagedSymbol, SYMBOL_FILLING_MODE);
   if((filling & SYMBOL_FILLING_IOC) != 0) return ORDER_FILLING_IOC;
   if((filling & SYMBOL_FILLING_FOK) != 0) return ORDER_FILLING_FOK;
   return ORDER_FILLING_RETURN;
}

//+------------------------------------------------------------------+
//| JSONパーサー                                                      |
//+------------------------------------------------------------------+
string ExtractJson(string json, string key)
{
   string search = "\"" + key + "\":";
   int pos = StringFind(json, search);
   if(pos < 0) return "";
   pos += StringLen(search);
   while(pos < StringLen(json) && StringGetCharacter(json, pos) == ' ') pos++;
   string result = "";
   bool isString = (StringGetCharacter(json, pos) == '"');
   if(isString) pos++;
   for(int i = pos; i < StringLen(json); i++)
   {
      ushort c = StringGetCharacter(json, i);
      if(isString && c == '"') break;
      if(!isString && (c == ',' || c == '}')) break;
      result += ShortToString(c);
   }
   return result;
}
//+------------------------------------------------------------------+
