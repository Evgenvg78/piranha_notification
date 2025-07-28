-- ����� ��� ����� (�������� � �������)
local LOG_DIR = "C:\\QUIK_VTB\\monitor\\logs\\"
-- ������������ ���� �������� ����� � ����
local LOG_RETENTION_DAYS = 30
-- �������� ���������� (� ��������)
local UPDATE_INTERVAL = 60

-- ������� ���� ��������� �����
local firmid = "SPBFUT"  -- ������������� �����
local client_code = "SPBFUT277A5"  -- ��� �������
local limit_kind = 0  -- ��� ������ (������ 0 ��� �������� �������, 2 ��� �������)

-- ������� ��� ��������� ���� � �������� ���-�����
function getLogFilePath()
    local dateStr = os.date("%Y-%m-%d")  -- �������� ���� � ������� "YYYY-MM-DD"
    return LOG_DIR .. "heartbeat_" .. dateStr .. ".log"
end

-- ������� ��� �������� ������ ���-������
function cleanOldLogs()
    local current_time = os.time()
    local retention_seconds = LOG_RETENTION_DAYS * 86400  -- 86400 ������ � ������

    for file in io.popen('dir "' .. LOG_DIR .. '" /b'):lines() do
        local filePath = LOG_DIR .. file
        local attr = io.popen('for %A in ("' .. filePath .. '") do @echo %~tA'):read("*l")

        if attr then
            -- ����������� ���� ����������� � timestamp
            local year, month, day = attr:match("(%d+)-(%d+)-(%d+)")
            if year and month and day then
                local file_time = os.time({ year = tonumber(year), month = tonumber(month), day = tonumber(day) })
                if (current_time - file_time) > retention_seconds then
                    os.remove(filePath)  -- ������� ����, ���� �� ������ ��������� �����
                    message("����� ������ ���: " .. filePath, 1)
                end
            end
        end
    end
end

-- ������� ��� ������� ����/����/����� �� �� ������� ��������
function getLongShortNetGO()
    local tbl      = "futures_client_holding"
    local myAcc    = client_code
    local class    = firmid
    local n        = getNumberOf(tbl)

    local longGO   = 0
    local shortGO  = 0

    for i = 0, n-1 do
        local r = getItem(tbl, i)
        if r and r.trdaccid == myAcc then
            local sec       = r.sec_code
            local totalnet  = tonumber(r.totalnet) or 0

            local buy_depo = getParamEx(class, sec, "BUYDEPO")
            local buy_val  = (buy_depo and buy_depo.result == "1") and tonumber(buy_depo.param_value) or 0
            local sell_depo = getParamEx(class, sec, "SELLDEPO")
            local sell_val  = (sell_depo and sell_depo.result == "1") and tonumber(sell_depo.param_value) or 0

            if totalnet > 0 then
                longGO = longGO + totalnet * buy_val
            elseif totalnet < 0 then
                shortGO = shortGO - math.abs(totalnet) * sell_val  -- ���������� � �������!
            end
        end
    end

    local netGO = longGO + shortGO
    return longGO, shortGO, netGO
end

-- ������� ��������� ������� �����
function getBalance()
    local portfolio = getPortfolioInfoEx(firmid, client_code, limit_kind)
    if portfolio then
        return tostring(portfolio.fut_asset)  -- ����������� � ������
    else
        return "N/A"  -- ���� ������ ���
    end
end

-- ������� ��������� ���������� � ��������� ������
function getLastTradeInfo()
    local num_trades = getNumberOf("all_trades")
    if num_trades > 0 then
        local last_trade = getItem("all_trades", num_trades - 1)
        if last_trade then
            local trade_time = "��� ������"
            if last_trade.datetime then
                trade_time = string.format("%02d:%02d:%02d",
                    last_trade.datetime.hour or 0,
                    last_trade.datetime.min or 0,
                    last_trade.datetime.sec or 0)
            end
            return string.format("%s; fut_code=%s; price=%s; volume=%s",
                trade_time,
                tostring(last_trade.sec_code),
                tostring(last_trade.price),
                tostring(last_trade.qty))
        end
    end
    return "��� ������"
end




-- ������� ��������� �������� ������ �������
function getPlannedNetPosition()
    local fut = getFuturesLimit(firmid, client_code, limit_kind, "SUR")
    if fut then
        return tostring(fut.cbplplanned)
    else
        return "N/A"
    end
end

-- ������� ������ � ���
function writeHeartbeatLog()
    local filePath = getLogFilePath()
    local file = io.open(filePath, "a")  -- "a" -> ���������� ������

    if file ~= nil then
        local timestamp = os.date("%Y-%m-%d %H:%M:%S")
        local connectionStatus = isConnected() and "true" or "false"
        local balance = getBalance()
        local plannedNetPosition = getPlannedNetPosition()
        local lastTradeInfo = getLastTradeInfo()
        local longGO, shortGO, netGO = getLongShortNetGO()

        -- ����������� ������ ���� � CSV-�����
        local logLine = string.format("%s;%s;%s;%s;%s;%s;%s;%s\n",
            timestamp,
            connectionStatus,
            balance,
            plannedNetPosition,
            lastTradeInfo,
            tostring(longGO),
            tostring(shortGO),
            tostring(netGO)
        )

        file:write(logLine)
        file:close()
    else
        message("������ �������� ����� ��� ������: " .. filePath, 3)
    end
end






-- �������� �������
function main()
    message("������� heartbeat logger � ����������� � �������", 1)

    while true do
        writeHeartbeatLog()
        --cleanOldLogs()
        sleep(UPDATE_INTERVAL * 1000)
    end
end

-- ���������� ���������
function OnStop()
    message("������ heartbeat_logger_with_trades ����������", 1)
end
