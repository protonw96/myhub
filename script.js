const canvas = document.getElementById('gameCanvas');
const ctx = canvas.getContext('2d');

// Адаптация размера под экран
function resize() {
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
}
window.addEventListener('resize', resize);
resize();

// Настройки игры
const gravity = 0.8;
let gameSpeed = 5;

class Player {
    constructor() {
        this.width = 50;
        this.height = 50;
        this.x = 50;
        this.y = canvas.height - this.height - 100;
        this.dy = 0; // Вертикальная скорость
        this.jumpForce = 15;
        this.grounded = false;
        this.color = '#00ffcc';
    }

    draw() {
        ctx.fillStyle = this.color;
        // Здесь можно использовать ctx.drawImage для спрайтов
        ctx.fillRect(this.x, this.y, this.width, this.height);
    }

    update() {
        // Гравитация
        if (this.y + this.height < canvas.height - 50) {
            this.dy += gravity;
            this.grounded = false;
        } else {
            this.dy = 0;
            this.grounded = true;
            this.y = canvas.height - 50 - this.height;
        }

        // Прыжок
        this.y += this.dy;
        this.draw();
    }

    jump() {
        if (this.grounded) {
            this.dy = -this.jumpForce;
            // Вибрация телефона при прыжке
            if (navigator.vibrate) navigator.vibrate(20);
        }
    }
}

const player = new Player();

// Отрисовка земли
function drawGround() {
    ctx.fillStyle = '#444';
    ctx.fillRect(0, canvas.height - 50, canvas.width, 50);
}

// Обработка касания (Touch)
window.addEventListener('touchstart', (e) => {
    player.jump();
    e.preventDefault(); // Чтобы не срабатывал клик после тапа
}, { passive: false });

// Также оставим поддержку клика для теста с ПК
window.addEventListener('mousedown', () => player.jump());

// Основной цикл игры
function animate() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    
    drawGround();
    player.update();

    requestAnimationFrame(animate);
}

animate();

