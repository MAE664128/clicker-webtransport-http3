let currentTransport; // Текущий транспорт.
let streamNumber; // Номер двунаправленного потока.
let currentTransportDatagramWriter; // Дескриптор для отправки дейтаграмм.
let currentBiStream; // Текущий (открытый) двунаправленный потока.
let readableBiStream;
let writableBiStream;
let biStreamWriter; // экземпляр для записи данных в двунаправленный поток.
let username; // Имя пользователя.
let currentUserScore; // Текущий счет пользователя.

const encoder = new TextEncoder('utf-8');
// Сообщение, которое будет отправляться на сервер при "кликах".
let msg = encoder.encode(JSON.stringify({
    type: "increment",
    username: "John Doe",
}));


// Выполнить инициализацию соединения.
async function connectWT() {
    document.getElementById('event-log').innerHTML = "";
    const url = document.getElementById('url').value;
    username = document.getElementById('username').value;
    msg = encoder.encode(JSON.stringify({
        type: "increment",
        username: username,
    }));
    try {
        var transport = new WebTransport(url);
        addToEventLog('Инициируем соединение...');
    } catch (e) {
        addToEventLog('Не удалось создать объект подключения. ' + e, 'error');
        return;
    }
    try {
        await transport.ready;
        addToEventLog('Подключение готово.');
    } catch (e) {
        addToEventLog('Соединение не удалось.' + e, 'error');
        return;
    }

    transport.closed
        .then(() => {
            addToEventLog('Соединение закрывается нормально.');
        })
        .catch(() => {
            addToEventLog('Соединение внезапно прервалось.', 'error');
        });

    currentTransport = transport;
    streamNumber = 1;

    await createDatagramWriter();
    readDatagrams(transport);

    await createBiStream();
    readFromIncomingStream();

    document.getElementById('sendDataFrame').disabled = false;
    document.getElementById('sendDataDStream').disabled = false;
    document.getElementById('connect').value = 'Reconnect';
}

// Выполнить отправку дейтаграммы.
async function sendDatagram() {
    try {
        await currentTransportDatagramWriter.write(msg);
        addToEventLog('Отправленная датаграмма: ' + msg);
    } catch (e) {
        addToEventLog('Ошибка при отправке данных: ' + e, 'error');
    }
}
// Инициировать экземпляр для отправки данных как дейтаграмму.
async function createDatagramWriter() {
    let transport = currentTransport;
    try {
        currentTransportDatagramWriter = transport.datagrams.writable.getWriter();
        addToEventLog('Устройство записи датаграмм готово.');
    } catch (e) {
        addToEventLog('Отправка датаграмм не поддерживается: ' + e, 'error');
        return;
    }
}

// Инициировать двунаправленный поток.
async function createBiStream() {
    let transport = currentTransport;
    try {
        currentBiStream = await transport.createBidirectionalStream();
        readableBiStream = currentBiStream.readable;
        writableBiStream = currentBiStream.writable;
        biStreamWriter = writableBiStream.getWriter();
    } catch (e) {
        addToEventLog('Ошибка при создании двунаправленного потока: ' + e, 'error');
    }
}

// Отправить данные в двунаправленный поток.
async function sendBiStream() {
    let biStream = currentBiStream;
    try {
        let writer = biStreamWriter;
        await writer.write(msg);
        addToEventLog(
            'Открытый двунаправленный поток #' + streamNumber +
            ' with msg: ' + msg
        );
    } catch (e) {
        addToEventLog('Ошибка при отправке данных в поток: ' + e, 'error');
    }
}

// Считывает датаграммы из |транспорта| до тех пор,
// пока не будет достигнут EOF.
async function readDatagrams(transport) {
  try {
    var reader = transport.datagrams.readable.getReader();
    addToEventLog('Считыватель датаграмм готов.');
  } catch (e) {
    addToEventLog('Прием датаграмм не поддерживается: ' + e, 'error');
    return;
  }
  let decoder = new TextDecoder('utf-8');
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        addToEventLog('Завершено чтение датаграмм!');
        return;
      }
      let data = decoder.decode(value);
      updateUserScore(data);
      addToEventLog('Датаграмма получена: ' + data);
    }
  } catch (e) {
    addToEventLog('Ошибка при чтении датаграмм: ' + e, 'error');
  }
}

// Считывает датаграммы из двунаправленного потока до тех пор,
// пока не будет достигнут EOF.
async function readFromIncomingStream() {
    let decoder = new TextDecoder('utf-8');
    const reader = readableBiStream.getReader();
    try {
        while (true) {
            const { value, done } = await reader.read();
            if (done) {
                addToEventLog('Stream #' + streamNumber + ' closed');
                return;
            }
            let data = decoder.decode(value);
            updateUserScore(data);
            addToEventLog('Полученные данные в потоке #' + streamNumber + ': ' + data);
        }
    } catch (e) {
        addToEventLog(
        'Error while reading from stream #' + streamNumber + ': ' + e, 'error');
        addToEventLog('    ' + e.message);
    }
}

// Добавить в лог сообщение.
function addToEventLog(text, severity = 'info') {
  let log = document.getElementById('event-log');
  let mostRecentEntry = log.lastElementChild;
  let entry = document.createElement('li');
  entry.innerText = text;
  entry.className = 'log-' + severity;
  log.appendChild(entry);

  if (mostRecentEntry != null &&
      mostRecentEntry.getBoundingClientRect().top <
          log.getBoundingClientRect().bottom) {
    entry.scrollIntoView();
  }
}


// Обновить текущий счет пользователя.
function updateUserScore(data) {
  let scoreElement = document.getElementById('userScore');
  let score = '?';
  try {
    let response = JSON.parse(data);
    score = response.score;
  } catch (_) {
    let response
  }
  scoreElement.innerText = score;
}