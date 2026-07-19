# Aplicativo Painel · Fire TV

Aplicativo Android TV/Fire TV que abre o painel de pedidos e aniversariantes em tela cheia.

## Compilar o APK

Abra esta pasta no Android Studio com o SDK Android instalado e execute:

```bash
./gradlew assembleDebug
```

O APK será gerado em `app/build/outputs/apk/debug/app-debug.apk`.

Na primeira execução, informe o usuário sem senha do perfil **Painel**. A sessão fica salva no aplicativo.

O Fire TV 3ª geração pode receber o APK por ADB para teste. Para publicação, gere uma versão assinada (`assembleRelease`) com um keystore permanente.
