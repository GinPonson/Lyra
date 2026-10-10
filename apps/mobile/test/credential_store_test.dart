import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';

void main() {
  test('同一凭据存储的操作按提交顺序执行，失败不阻塞退出清理', () async {
    final store = MemoryCredentialStore();
    final release = Completer<void>();
    final steps = <String>[];
    final first = withCredentialMutation(store, () async {
      steps.add('write-start');
      await release.future;
      steps.add('write-failed');
      throw StateError('secure storage unavailable');
    });
    final failed = expectLater(first, throwsStateError);
    final next = withCredentialMutation(store, () async => steps.add('clear'));
    await pumpEventQueue();
    expect(steps, ['write-start']);
    release.complete();
    await failed;
    await next;
    expect(steps, ['write-start', 'write-failed', 'clear']);
  });

  test('所有 SecureCredentialStore 实例共享串行边界', () async {
    final firstStore = SecureCredentialStore();
    final secondStore = SecureCredentialStore();
    final release = Completer<void>();
    var secondStarted = false;
    final first = withCredentialMutation(firstStore, () => release.future);
    final second = withCredentialMutation(secondStore, () async {
      secondStarted = true;
    });
    await pumpEventQueue();
    expect(secondStarted, isFalse);
    release.complete();
    await first;
    await second;
    expect(secondStarted, isTrue);
  });

  test('执行中的操作同步排入下一项时也保持串行', () async {
    final store = MemoryCredentialStore();
    final release = Completer<void>();
    final steps = <String>[];
    late Future<void> next;
    final first = withCredentialMutation(store, () async {
      steps.add('first-start');
      next = withCredentialMutation(store, () async => steps.add('second'));
      await release.future;
      steps.add('first-finish');
    });
    await pumpEventQueue();
    expect(steps, ['first-start']);
    release.complete();
    await first;
    await next;
    expect(steps, ['first-start', 'first-finish', 'second']);
  });

  test('互不相干的内存凭据存储不等待另一个登录流程', () async {
    final blocked = Completer<void>();
    final first = withCredentialMutation(
      MemoryCredentialStore(),
      () => blocked.future,
    );
    var secondStarted = false;
    await withCredentialMutation(MemoryCredentialStore(), () async {
      secondStarted = true;
    });
    expect(secondStarted, isTrue);
    blocked.complete();
    await first;
  });
}
