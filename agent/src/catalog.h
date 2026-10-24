#pragma once
// Katalog aplikací na VM.
// Rozložení:  <catalogDir>/<Type>/<version>/manifest.json
// manifest:   {"type":"App2","version":"2.4.1","executable":"bin/App2","args":[]}
// Instalaci verzí do katalogu dělá deploy (např. GitLab CI job), ne agent.

#include <QDir>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QMap>
#include <QStringList>
#include <QVersionNumber>

struct CatalogEntry {
    QString type;
    QString version;
    QString executable;   // absolutní cesta
    QStringList args;
};

class Catalog
{
public:
    explicit Catalog(QString dir) : dir_(std::move(dir)) {}

    void rescan()
    {
        entries_.clear();
        QDir root(dir_);
        for (const QString &type : root.entryList(QDir::Dirs | QDir::NoDotAndDotDot)) {
            QDir typeDir(root.filePath(type));
            for (const QString &ver : typeDir.entryList(QDir::Dirs | QDir::NoDotAndDotDot)) {
                QFile f(typeDir.filePath(ver + "/manifest.json"));
                if (!f.open(QIODevice::ReadOnly))
                    continue;
                const QJsonObject m = QJsonDocument::fromJson(f.readAll()).object();
                CatalogEntry e;
                e.type = m.value("type").toString(type);
                e.version = m.value("version").toString(ver);
                e.executable = QDir(typeDir.filePath(ver)).absoluteFilePath(m.value("executable").toString());
                for (const auto &a : m.value("args").toArray())
                    e.args << a.toString();
                if (QFile::exists(e.executable))
                    entries_[key(e.type, e.version)] = e;
            }
        }
    }

    bool has(const QString &type, const QString &version) const { return entries_.contains(key(type, version)); }
    CatalogEntry get(const QString &type, const QString &version) const { return entries_.value(key(type, version)); }

    // [{type, versions:[nejnovější první]}]
    QJsonArray toJson() const
    {
        QMap<QString, QStringList> raw;
        for (const auto &e : entries_)
            raw[e.type] << e.version;
        QJsonArray out;
        for (auto it = raw.begin(); it != raw.end(); ++it) {
            QStringList v = it.value();
            std::sort(v.begin(), v.end(), [](const QString &a, const QString &b) {
                return QVersionNumber::fromString(a) > QVersionNumber::fromString(b);
            });
            out.append(QJsonObject{{"type", it.key()}, {"versions", QJsonArray::fromStringList(v)}});
        }
        return out;
    }

private:
    static QString key(const QString &t, const QString &v) { return t + '@' + v; }
    QString dir_;
    QMap<QString, CatalogEntry> entries_;
};
