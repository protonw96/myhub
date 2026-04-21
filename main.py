import pygame
import random
import os
import asyncio

# Настройки
WIDTH, HEIGHT = 480, 600
FPS = 60

pygame.init()
screen = pygame.display.set_mode((WIDTH, HEIGHT))
clock = pygame.time.Clock()

def load_img(name, w, h):
    path = os.path.join('assets', 'img', name)
    try:
        img = pygame.image.load(path).convert_alpha()
        return pygame.transform.scale(img, (w, h))
    except:
        surf = pygame.Surface((w, h))
        surf.fill((200, 0, 0))
        return surf

# Загрузка ресурсов
background_img = load_img("starfield.png", WIDTH, HEIGHT)
player_img = load_img("player.png", 50, 50)
meteor_img = load_img("meteor.png", 45, 45)
laser_img = load_img("laser.png", 8, 25)

class Player(pygame.sprite.Sprite):
    def __init__(self):
        super().__init__()
        self.image = player_img
        self.rect = self.image.get_rect(midbottom=(WIDTH//2, HEIGHT - 20))
        self.speed = 7

    def update(self):
        keys = pygame.key.get_pressed()
        # Управление кнопками
        if keys[pygame.K_LEFT]: self.rect.x -= self.speed
        if keys[pygame.K_RIGHT]: self.rect.x += self.speed
        
        # Управление тачем (нажатие в левой/правой части экрана)
        mouse_pos = pygame.mouse.get_pos()
        if pygame.mouse.get_pressed()[0]:
            if mouse_pos[0] < WIDTH // 2: self.rect.x -= self.speed
            else: self.rect.x += self.speed
            
        self.rect.clamp_ip(screen.get_rect())

class Meteor(pygame.sprite.Sprite):
    def __init__(self):
        super().__init__()
        self.image = meteor_img
        self.rect = self.image.get_rect(x=random.randint(0, WIDTH-45), y=-50)
        self.speed = random.randint(3, 6)

    def update(self):
        self.rect.y += self.speed
        if self.rect.top > HEIGHT: self.kill()

class Bullet(pygame.sprite.Sprite):
    def __init__(self, x, y):
        super().__init__()
        self.image = laser_img
        self.rect = self.image.get_rect(center=(x, y))

    def update(self):
        self.rect.y -= 10
        if self.rect.bottom < 0: self.kill()

async def main():
    player = Player()
    all_sprites = pygame.sprite.Group(player)
    meteors = pygame.sprite.Group()
    bullets = pygame.sprite.Group()
    
    last_shot = pygame.time.get_ticks()
    score = 0

    running = True
    while running:
        screen.blit(background_img, (0,0))
        
        for event in pygame.event.get():
            if event.type == pygame.QUIT: running = False

        # Автострельба
        now = pygame.time.get_ticks()
        if now - last_shot > 400:
            b = Bullet(player.rect.centerx, player.rect.top)
            all_sprites.add(b); bullets.add(b)
            last_shot = now

        # Спавн врагов
        if random.random() < 0.03:
            m = Meteor()
            all_sprites.add(m); meteors.add(m)

        all_sprites.update()

        # Столкновения
        if pygame.sprite.groupcollide(meteors, bullets, True, True):
            score += 1
        if pygame.sprite.spritecollide(player, meteors, True):
            running = False # Проигрыш

        all_sprites.draw(screen)
        pygame.display.flip()
        await asyncio.sleep(0) # Критично для веба
        clock.tick(FPS)

asyncio.run(main())

