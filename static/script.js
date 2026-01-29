function onlyDigits(s) {
  return (s || "").replace(/\D/g, "");
}

function setMsg(text, ok) {
  const el = document.getElementById("msg");
  el.classList.remove("hidden", "ok", "err");
  el.classList.add(ok ? "ok" : "err");
  el.textContent = text;
}

async function send(acao) {
  const cpf = onlyDigits(document.getElementById("cpf").value);
  const ultimos = onlyDigits(document.getElementById("ultimos").value);

  if (cpf.length !== 11) {
    setMsg("CPF deve ter 11 dígitos.", false);
    return;
  }
  if (ultimos.length < 2) {
    setMsg("Informe os últimos dígitos (ex.: 4).", false);
    return;
  }

  try {
    const resp = await fetch("/check", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ cpf, ultimos_digitos: ultimos, acao })
    });

    const data = await resp.json().catch(() => ({}));

    if (!resp.ok) {
      setMsg(data.detail || "Não foi possível registrar. Verifique os dados.", false);
      return;
    }

    setMsg(data.message || `Registrado: ${acao}`, true);
  } catch (e) {
    setMsg("Falha de conexão com o servidor.", false);
  }
}

document.getElementById("btnEntrada").addEventListener("click", () => send("entrada"));
document.getElementById("btnSaida").addEventListener("click", () => send("saida"));
