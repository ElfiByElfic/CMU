#pragma once
// libchainapp – povinná část REST API pro každou aplikaci v řetězci.
// Aplikace knihovnu přilinkuje, zaregistruje své kontroly a konfiguraci
// a zavolá start(). Knihovna sama obslouží /api/v1/health/*, /info, /status,
// /metrics, /config a /control/* podle api/app-api.yaml.

#include <QObject>
#include <QJsonObject>
#include <QJsonValue>
#include <QString>
#include <functional>
#include <memory>

class QHttpServer;
class QHttpServerRequest;

namespace chainapp {

// Jednotný stavový model (stejné hodnoty jako AppState v app-api.yaml).
enum class State { Starting, Running, Degraded, Paused, Draining, Stopping, Error };
QString toString(State s);

struct CheckResult {
    bool ok = true;
    QString detail;
};

// Identita a spouštěcí kontrakt – vše dodává agent v proměnných prostředí.
struct Identity {
    QString instanceId;     // CHAIN_INSTANCE_ID   např. vm-prod-02/chain-a/app2
    QString chainId;        // CHAIN_ID
    int position = 0;       // CHAIN_POSITION      0 = vstup řetězce
    QString upstreamUrl;    // CHAIN_UPSTREAM_URL  prázdné u vstupu
    QString downstreamUrl;  // CHAIN_DOWNSTREAM_URL prázdné u výstupu
    QString bindAddress;    // APP_BIND_ADDRESS    výchozí 127.0.0.1
    quint16 port = 0;       // APP_PORT
    QString apiToken;       // APP_API_TOKEN
    QString configFile;     // APP_CONFIG_FILE     JSON s konfigurací od agenta
    QString dataDir;        // APP_DATA_DIR        vlastní adresář instance
    QString appType;        // z konstruktoru
    QString version;        // z konstruktoru
    QString build;          // z konstruktoru
};

class ChainApp : public QObject
{
    Q_OBJECT
public:
    static constexpr const char *CorrelationHeader = "X-Correlation-ID";

    ChainApp(const QString &appType, const QString &version,
             const QString &build = {}, QObject *parent = nullptr);
    ~ChainApp() override;

    // --- registrace (volat PŘED start()) -----------------------------------
    // Kontrola pro /health/ready, např. dostupnost DB nebo downstreamu.
    void addReadinessCheck(const QString &name, std::function<CheckResult()> check);

    // Konfigurační klíč. Hodnotu z APP_CONFIG_FILE knihovna načte při start().
    // validator vrací prázdný řetězec = OK, jinak text chyby.
    void addConfig(const QString &key, const QJsonValue &defaultValue,
                   bool runtimeEditable, bool secret = false,
                   std::function<QString(const QJsonValue &)> validator = {});

    // Doplňkové údaje do /status (pole "extra").
    void setStatusExtra(std::function<QJsonObject()> provider);

    // --- start -------------------------------------------------------------
    // Načte prostředí a konfiguraci, spustí HTTP server. Stav = Starting.
    // Až je aplikace připravená, zavolejte setState(State::Running).
    bool start(QString *error = nullptr);

    const Identity &identity() const;
    QHttpServer *server();   // pro vlastní, aplikačně specifické routy

    // --- běh ---------------------------------------------------------------
    void setState(State s, const QString &message = {});
    State state() const;

    QJsonValue config(const QString &key) const;

    void markProcessed(qint64 n = 1);
    void markError(qint64 n = 1);
    void setQueueDepth(qint64 depth);
    void incCounter(const QString &name, double by = 1.0);  // Prometheus counter
    void setGauge(const QString &name, double value);       // Prometheus gauge

    // Korelační ID z příchozího požadavku, případně nově vygenerované.
    // Posílejte ho dál v hlavičce CorrelationHeader na downstream.
    static QByteArray correlationId(const QHttpServerRequest &req);

signals:
    void configChanged(const QString &key, const QJsonValue &value);
    // Přestaň přijímat nové zprávy a dokonči rozpracované, pak zavolej drainFinished().
    // Když signál není připojený, knihovna považuje drain za okamžitě hotový.
    void drainRequested();
    void pauseRequested();
    void resumeRequested();
    void reloadRequested();

public slots:
    void drainFinished();

private:
    struct Private;
    std::unique_ptr<Private> d;
};

} // namespace chainapp
